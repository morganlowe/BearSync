# Google Cloud OAuth 2.0 Credentials Setup Guide

BearSync is a private, lightweight, native Linux sync client for Google Drive. To protect your privacy and ensure your files are transferred directly between your machine and Google's servers without any intermediate third-party servers or rate throttling, BearSync uses your own personal Google Cloud OAuth 2.0 desktop credentials.

Google Cloud API access for personal use is **100% free** and includes 1 billion API requests per day.

---

## Step-by-Step Walkthrough (Takes ~2 Minutes)

### Step 1: Open Google Cloud Console
1. In your browser, navigate to **[Google Cloud Console](https://console.cloud.google.com/)**.
2. Sign in with the Google account whose Drive you want to sync.

---

### Step 2: Create a Project
1. In the top navigation bar, click the project selection dropdown.
2. Click **New Project** in the upper right corner.
3. Project Name: **`BearSync`** (or any name you prefer).
4. Click **Create** and ensure your new project is selected.

---

### Step 3: Enable the Google Drive API
1. Open the left sidebar menu (☰) and navigate to **APIs & Services &rarr; Library**.
2. In the search box, search for **`Google Drive API`**.
3. Select **Google Drive API** and click **Enable**.

---

### Step 4: Configure the OAuth Consent Screen
1. In the left navigation, click **OAuth consent screen** (or **Branding / Audience**).
2. User Type: Select **External** and click **Create**.
3. Fill in the required fields:
   - **App name**: `BearSync`
   - **User support email**: Your email address
   - **Developer contact information**: Your email address
4. Click **Save and Continue** through the Scopes step (no manual scopes needed).
5. Under **Test users**, click **+ Add Users**, enter your Google email address, and click **Add**.
   > **Note:** Adding yourself as a test user allows you to log in smoothly while your project is in testing mode.
6. Click **Save and Continue** to finish.

---

### Step 5: Generate Desktop OAuth Credentials
1. In the left navigation, click **Credentials**.
2. At the top of the page, click **+ Create Credentials &rarr; OAuth client ID**.
3. Under **Application type**, select **Desktop app**.
4. Name: **`BearSync Desktop`**.
5. Click **Create**.

---

### Step 6: Link to BearSync
A dialog will pop up showing your **Client ID** and **Client Secret**.

You can either:
- **Option A (Instant Form Input):** Copy the **Client ID** and **Client Secret** and paste them directly into BearSync's Setup Wizard.
- **Option B (JSON File):** Click **Download JSON** in Google Cloud Console, then in BearSync click **Load client_secret.json...**.

Click **Save Credentials**, then click **Sign in with Google (Open Web Browser)** to link your Drive!

---

## Environment Variables (Optional / Headless)

For scripted installations or containerized setups, you can also set the credentials via environment variables:

```bash
export BEARSYNC_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export BEARSYNC_CLIENT_SECRET="GOCSPX-your-client-secret"
bearsync
```
