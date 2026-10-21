"""Read terminal usage without buffering or modifying streamed model output."""

import codecs
import json

from .budget_pricing import usage_tokens


class UsageStream:
    def __init__(self, provider):
        self.provider = provider
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.pending = ""
        self.usage = {}
        self.complete = False
        self.final_usage = False
        self.invalid = False

    def feed(self, chunk):
        if self.invalid:
            return
        try:
            self.pending += self.decoder.decode(chunk)
            # Bound an event, not the entire conversation. Oversized/unknown events
            # never justify releasing a reservation.
            if len(self.pending) > 2_000_000:
                raise ValueError("Oversized usage event")
            self.pending = self.pending.replace("\r\n", "\n")
            while "\n\n" in self.pending:
                event, self.pending = self.pending.split("\n\n", 1)
                data = "\n".join(
                    line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:")
                )
                if data and data != "[DONE]":
                    self.event(json.loads(data))
        except (ValueError, TypeError, KeyError, AttributeError):
            self.invalid = True

    def event(self, value):
        kind = value.get("type")
        if self.provider == "anthropic":
            if kind == "message_start":
                if self.usage:
                    raise ValueError("Duplicate message")
                self.usage = value["message"]["usage"]
            elif kind == "message_delta":
                delta = value.get("usage", {})
                if type(delta.get("output_tokens")) is not int:
                    raise ValueError("Missing terminal output usage")
                self.final_usage = True
                self.usage.update(delta)
            elif kind == "message_stop":
                self.complete = self.final_usage
            elif kind == "error":
                self.invalid = True
        elif kind in {"response.completed", "response.incomplete"}:
            self.usage = value["response"]["usage"]
            self.complete = True
        elif kind in {"error", "response.failed"}:
            self.invalid = True

    def tokens(self):
        if not self.complete or self.invalid or self.decoder.getstate()[0]:
            raise ValueError("No trustworthy terminal usage; reservation retained")
        return usage_tokens(self.provider, self.usage)
