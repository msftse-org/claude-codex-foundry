import azure.functions as func

from claude_foundry.portal.app import create_app

portal = create_app()
app = func.AsgiFunctionApp(app=portal, http_auth_level=func.AuthLevel.ANONYMOUS)


@app.timer_trigger(
    schedule="0 */30 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True
)
def budget_recovery_checkpoint(timer: func.TimerRequest):
    if portal.state.recovery_checkpoint is not None:
        portal.state.recovery_checkpoint()
