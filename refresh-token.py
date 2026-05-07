from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
import os

SCOPES = [
    "https://www.googleapis.com/auth/adwords",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email"
]

def main():
    creds = None

    if os.path.exists('token1.json'):
        creds = Credentials.from_authorized_user_file('token1.json', SCOPES)

    # If no valid credentials or refresh token, start flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None
        
        if not creds or not creds.valid:
            # Create a new flow
            flow = InstalledAppFlow.from_client_secrets_file(
                'client-secrets-web.json',
                SCOPES
            )
            creds = flow.run_local_server(
                port=5000,
                authorization_prompt_message='Please authorize this app',
                access_type='offline',
                prompt='consent'
            )

        # Save credentials for future use
        with open('token1.json', 'w') as token:
            token.write(creds.to_json())

    print("Refresh Token:", creds.refresh_token)

if __name__ == '__main__':
    main()
