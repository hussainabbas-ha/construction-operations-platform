import os
import logging
from typing import List, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
import io

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURATION FOR GOULD CONSTRUCTION DRIVE
# ==========================================
# If you need to actually download file contents (not just list them), 
# you should change this to 'https://www.googleapis.com/auth/drive.readonly'
# Note: Changing scopes requires deleting the existing 'token.json' and re-authenticating.
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

TARGET_FOLDER_ID = "0AP9QW31FGsN1Uk9PVA"
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


class GoogleDriveClient:
    """
    A base client for interacting with the the construction operations platform Google Drive.
    
    This client is configured to target a specific Shared Drive/Folder.
    
    Dev Note: This currently uses interactive OAuth flow (Client ID). For a headless 
    production environment (e.g., automated server tasks), consider swapping this 
    authentication method to use a Google Service Account (.json key) instead.
    """
    
    def __init__(self, credentials_path: str = CREDENTIALS_FILE, token_path: str = TOKEN_FILE):
        # Resolve paths relative to the project root assuming this runs from the root
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = self._authenticate()

    def _authenticate(self):
        """Authenticate and return the Google Drive API service."""
        creds = None
        
        # 1. Try to load existing token
        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            except Exception as e:
                logger.error(f"Error loading token: {e}")

        # 2. If no valid credentials, run OAuth flow
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Google Drive token...")
                creds.refresh(Request())
            else:
                logger.info("Starting new OAuth flow for Google Drive...")
                if not os.path.exists(self.credentials_path):
                    raise FileNotFoundError(
                        f"Credentials file '{self.credentials_path}' not found. "
                        "Please ensure it is located in the project root."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                # This opens a browser window for local interactive login
                creds = flow.run_local_server(port=0)
            
            # Save the new credentials for future runs
            with open(self.token_path, 'w') as token_file:
                token_file.write(creds.to_json())

        return build('drive', 'v3', credentials=creds)

    def list_target_folder_files(self, page_size: int = 50) -> List[Dict[str, str]]:
        """
        Lists files inside the specific the construction operations platform target folder.
        
        Returns:
            A list of dictionaries containing 'id' and 'name' of the files.
        """
        try:
            results = self.service.files().list(
                q=f"'{TARGET_FOLDER_ID}' in parents",
                # The following two flags are required when dealing with Shared Drives
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=page_size,
                fields="nextPageToken, files(id, name)"
            ).execute()
            
            return results.get('files', [])
            
        except HttpError as error:
            logger.error(f"An error occurred accessing Google Drive API: {error}")
            return []

    def list_folder(self, folder_id: str, page_size: int = 100) -> List[Dict[str, str]]:
        """Lists direct children of any folder by ID."""
        try:
            results = self.service.files().list(
                q=f"'{folder_id}' in parents",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=page_size,
                fields="files(id, name, mimeType)"
            ).execute()
            return results.get('files', [])
        except HttpError as error:
            logger.error(f"Error listing folder {folder_id}: {error}")
            return []

    def download_file(self, file_id: str, dest_path: str) -> bool:
        """
        Downloads a specific file by its ID to the specified destination path.
        
        Returns:
            True if download is successful, False otherwise.
        """
        try:
            # We must specify acknowledgeAbuse or similar if needed, 
            # but for standard files get_media is fine.
            request = self.service.files().get_media(fileId=file_id)
            
            # Using io.FileIO to write the file in chunks
            fh = io.FileIO(dest_path, mode='wb')
            downloader = MediaIoBaseDownload(fh, request)
            
            done = False
            logger.info(f"Starting download for file ID: {file_id}")
            while done is False:
                status, done = downloader.next_chunk()
                if status:
                    logger.info(f"Download {int(status.progress() * 100)}%.")
            
            logger.info(f"Download complete: {dest_path}")
            return True
            
        except HttpError as error:
            logger.error(f"An error occurred while downloading: {error}")
            return False


if __name__ == "__main__":
    AWARDED_FOLDER_ID = "1XZkfZjNMBPcOqQ9hmVSz7W-Z-nC_HspD"
    FOLDER_MIME = "application/vnd.google-apps.folder"

    client = GoogleDriveClient()

    import re
    JOB_PATTERN = re.compile(r'^\d+\w*\-')

    YEAR_PATTERN = re.compile(r'^\d{4}$')

    def print_job_folders(folder_id):
        children = client.list_folder(folder_id)
        for f in children:
            if f['mimeType'] == FOLDER_MIME:
                if JOB_PATTERN.match(f['name']):
                    print(f"\n  {f['name']}")
                    job_children = client.list_folder(f['id'])
                    for c in job_children:
                        if c['mimeType'] == FOLDER_MIME:
                            print(f"    [Folder] {c['name']}")
                        else:
                            print(f"    [File] {c['name']}")
                elif YEAR_PATTERN.match(f['name']):
                    print(f"\n{f['name']}")
                    print_job_folders(f['id'])

    print("\n--- Awarded Project Managment (Job Folders) ---")
    print_job_folders(AWARDED_FOLDER_ID)
