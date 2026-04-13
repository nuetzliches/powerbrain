"""Office 365 adapter — syncs SharePoint, OneDrive, OneNote, Outlook, and Teams
into the Powerbrain knowledge base via Microsoft Graph API.

Separate package with its own dependencies (msgraph-sdk, msal, markitdown).
Only import from ingestion core: SourceAdapter, NormalizedDocument, FileChange.
"""

from ingestion.adapters.office365.adapter import Office365Adapter, Office365Config

__all__ = ["Office365Adapter", "Office365Config"]
