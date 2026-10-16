# Telegram AI Voice Bot Platform (FastAPI + Vercel + Kiritts)

This starter project provides:
- **Bot 1**: your main AI text-to-speech bot.
- **Bot 2**: a manager bot where a user submits their own Telegram bot token.
- The manager validates the token with Telegram, encrypts it in Supabase, and registers a dedicated webhook.
- Connected bots use your server-side Kiritts API key to generate MP3 audio.

## Important security notes
- A Telegram bot token is a password. Never commit tokens, `.env` files, or service-role keys to GitHub.
- Users submit their bot token to Bot 2 in a private chat. Treat this as sensitive information.
- The bot token is encrypted at rest with Fernet. Keep `FERNET_KEY` secret and backed up. If it is lost, saved tokens cannot be decrypted.
- Supabase's service-role key bypasses row-level security; keep it only in Vercel environment variables.
- This starter has no usage quotas or billing controls. Add those before making the service public to prevent API abuse and unexpected costs.
- Tell users how to disconnect their bot and delete their stored record before running this as a public service.

## 1. Create the two Telegram bots
Use `@BotFather` to create:
1. Bot 1 — your main AI Voice bot.
2. Bot 2 — the Bot Manager.

Copy their tokens into Vercel environment variables, not into this repository.

## 2. Create a Supabase database
Create a Supabase project, then open SQL Editor and run `schema.sql`.
Copy the project URL and service-role key from Project Settings -> API.

## 3. Create the Fernet encryption key
On a computer with Python and cryptography installed, run:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
On Termux, install the dependency first if needed:
```bash
pkg update
pkg install python
pip install cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 4. Deploy to Vercel
Upload these files to a GitHub repository. Do not upload `.env` or real keys.
Vercel detects the root `index.py` FastAPI app and installs `requirements.txt`.

In Vercel -> Project -> Settings -> Environment Variables, add every variable in `.env.example`.
Set `APP_BASE_URL` to the actual production deployment URL, e.g. `https://your-project.vercel.app`.
Redeploy after setting environment variables.

## 5. Register the main and manager webhooks
Call the endpoint once after deployment. Replace the URL and setup secret:
```bash
curl -X POST "https://your-project.vercel.app/setup-webhooks" \
  -H "x-setup-secret: YOUR_SETUP_SECRET"
```
Expected response includes `"ok": true`. If you receive an error, check Vercel logs and environment variables. Never paste secret values into a public chat.

## 6. Test
- Open Bot 1 and send `/start`, then send a short text.
- Open Bot 2 and send `/start`.
- In Bot 2's **private chat**, send the token of a test bot you own.
- Once the manager says it connected, open that test bot and send `/start`, then a short text.
- The connected bot should return an MP3 audio file.

## Routes
- `GET /` health/info
- `GET /health`
- `POST /setup-webhooks` protected by `x-setup-secret`
- `POST /webhook/main`
- `POST /webhook/manager`
- `POST /webhook/{bot_key}` for connected bots

## Limitations before public launch
This is a starter implementation, not a finished multi-tenant SaaS. Before inviting users:
- Add per-user rate limits and monthly usage quotas.
- Add a secure disconnect command and delete the associated Supabase row.
- Add admin monitoring and abuse prevention.
- Review Kiritts pricing, rate limits, and terms.
- Consider a safer OAuth-like setup if available; Telegram BotFather tokens are bearer credentials.
- Add a user consent and privacy notice explaining that submitted tokens are stored encrypted.
# BOTSOMLENG
