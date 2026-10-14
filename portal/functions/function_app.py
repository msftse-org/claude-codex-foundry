import azure.functions as func

from claude_foundry.portal.app import create_app

app = func.AsgiFunctionApp(app=create_app(), http_auth_level=func.AuthLevel.ANONYMOUS)
