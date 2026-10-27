# Screenshot provenance

The original walkthrough JPEGs were captured on **2026-10-14** from the real Azure-hosted Claude & Codex Admin preview, using the owner's signed-in Microsoft Edge session. The application build was based on commit `4ff8c720bc723a5451a1de6404192633adc34efd`. The sign-in capture used a separate unsigned-in tab without signing out the owner's session.

`portal-security-status.jpg` was captured later on the same date from the deployed code in `10bd051`, after the hosted managed identity successfully published the preview policy. It shows the real published/connected status and excludes both pointers and operator identifiers.

The environment is an isolated preview, not a customer production release. Usage comes from small real verification requests, including deliberate 401/403 tests. Empty request lists and unset budgets are genuine current state. Capturing the images did not save form changes or publish gateway policies.

Images were cropped at capture time to exclude operator identities, email addresses, tenant/subscription IDs, gateway addresses, and browser chrome. Both the physical pointer and automation pointer are outside the final capture regions. Screenshots are not generated mockups and values have not been edited.

`portal-publication-review.jpg` intentionally includes only the impact section; `portal-audit.jpg` includes the action/target columns. Drawer images show their visible scroll region. The nine-model catalog is environment-specific and does not imply availability in another subscription.

When updating screenshots, follow [CONTRIBUTING.md](../../CONTRIBUTING.md#documentation-and-screenshot-standards). Save private originals outside Git, check the actual saved files, and update the capture date/build provenance. The [portal guide](../portal-guide.md) supplies captions, feature context, and limitations for each image.

`portal-content-response.jpg` was captured on **2026-10-15** from the deployed `feat/codex-azure` content-log preview. It shows a real synthetic Claude streaming response retrieved through the portal managed identity. Access was audited. The capture-time crop excludes both pointers, request/developer identifiers, and browser chrome; message values have not been edited.

`enrollment-automatic-refresh.png` was captured on **2026-10-27** from the
actual `EnrollmentOnboarding` component on `codex/default-configuration-refresh`
using a local Vite UI fixture and headless Chrome. The fixture supplies synthetic
enrollment data, not a deployed backend or real sign-in. The capture-time crop
shows only the setup instructions, excluding URLs, identifiers and pointers.
The wording is rendered by the application unchanged. Desktop and 390-pixel
layouts were checked; this is copy/layout evidence, not native-client,
Entra or launchd qualification.
