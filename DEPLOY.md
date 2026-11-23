# Putting the RPH Generator online (free)

You need three free accounts: GitHub (holds the code), Google Cloud (for "Sign in with Google"), and Render (runs the app). About 20 minutes, once.

## 1. GitHub
Create an empty repository, e.g. `rph-generator`, and push this folder to it.

## 2. Render (the server)
1. Sign in at https://render.com with GitHub.
2. **New > Blueprint**, pick the repository. Render reads `render.yaml` and builds the Docker image (about 5 to 10 minutes the first time).
3. Note the app address, e.g. `https://rph-generator.onrender.com`.

The free plan sleeps after 15 minutes without visitors; the first visit after that takes about a minute to wake up.

## 3. Google sign-in
1. https://console.cloud.google.com > create a project, e.g. "RPH Generator".
2. **APIs & Services > Library**: enable **Google Drive API**.
3. **OAuth consent screen**: External, app name "RPH Generator", your email. Scopes: `openid`, `email`, `profile`, `.../auth/drive.file`. While in **Testing**, add each teacher's Gmail under **Test users** (up to 100).
4. **Credentials > Create credentials > OAuth client ID**: Web application. Authorized redirect URI: `https://<your-app>.onrender.com/auth/callback`.
5. Copy the Client ID and Client secret.

## 4. Connect them
In Render > your service > **Environment**, set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, then **Manual deploy**. Open the app address and sign in.

## Opening it to all teachers
In Testing mode only listed test users can sign in. To let any teacher in:

1. In Render > **Environment**, add `SUPPORT_EMAIL` (shown on the privacy page as the contact address) and redeploy.
2. In Google Cloud > **Google Auth Platform > Branding**, fill in:
   - App home page: `https://<your-app>.onrender.com/`
   - Privacy policy: `https://<your-app>.onrender.com/privacy`
   - Terms of service: `https://<your-app>.onrender.com/terms`
   - Authorized domains: `<your-app>.onrender.com`
3. **Audience > Publish app**. `drive.file` is not a sensitive scope, so no long review is needed. It is free.
