# Deploying Drishti

Drishti is a static website: hosting it means publishing the `viewer/` folder. This guide uses **Firebase Hosting** (Google Cloud), project `drishti-sars-493bb`. Any static host works the same way with the `dist/` folder.

## One-time setup

1. **Project and billing.** In the [Firebase console](https://console.firebase.google.com), open the project and choose **Upgrade → Blaze**, linked to the billing account with the Google Cloud credit. The free Spark plan only allows 360 MB of traffic a day, and one visit to the Gorkha dataset downloads about 196 MB.
2. **Budget alert.** In [Google Cloud Console → Billing → Budgets & alerts](https://console.cloud.google.com/billing), create a budget (for example 10 EUR, alerts at 50 / 90 / 100 %).
3. **Team access.** Firebase console → Project settings → **Users and permissions** → add each deployer's Google account as **Owner** or **Firebase Hosting Admin**.
4. **Tools.** Install [Node.js](https://nodejs.org) 18 or newer, then:
   ```bash
   npm install -g firebase-tools
   firebase login
   ```

## Deploy

From the repository root:

```bash
python scripts/build_site.py          # builds dist/ from viewer/ (no Google Maps key)
firebase deploy --only hosting        # publishes dist/
```

The site is live at **https://drishti-sars-493bb.web.app** (and `.firebaseapp.com`). `firebase.json` sets the caching rules; `.firebaserc` names the project.

`build_site.py` never changes `viewer/`. It leaves out the local Google Maps key, old dataset copies and the dev server, and checks that every dataset in the menu is complete.

### Google Maps basemap (optional)

A browser key is visible to every visitor, so restrict it first: Google Cloud Console → **APIs & Services → Credentials** → your key:

- **Application restrictions → Websites:** `https://YOUR-DOMAIN/*`, `https://drishti-sars-493bb.web.app/*`, `http://localhost:8000/*`
- **API restrictions:** Maps JavaScript API

Then put the key in `viewer/config.js` (copy of `viewer/config.example.js`, never committed) and build with:

```bash
python scripts/build_site.py --with-maps-key
firebase deploy --only hosting
```

## Custom domain

1. Claim the domain (Space Apps Global Offer: GoDaddy Registry, registered through Porkbun).
2. Firebase console → **Hosting → Add custom domain** → enter the domain (and `www.` if wanted).
3. Firebase lists DNS records (a TXT record for verification and A records). Add them in Porkbun → **Domain Management → DNS**, replacing any default A records for the same name.
4. Firebase verifies the domain and issues the HTTPS certificate automatically, usually within an hour (up to 24 hours).

## Updating the site

After changing `viewer/` (new data, a new Gemini explanation, UI changes):

```bash
python scripts/build_site.py
firebase deploy --only hosting
```

To undo a bad deploy: Firebase console → Hosting → **Release history** → roll back.

## Cost

Storage (about 360 MB) is within the free allowance. Traffic beyond the free daily amount costs about 0.15 USD per GB; 100 full visits to every dataset is roughly 35 GB, about 5 USD, taken from the credit.
