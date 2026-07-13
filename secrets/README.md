# secrets/

Drop `cookies.txt` here to get yt-dlp past YouTube's bot challenge.

YouTube challenges datacenter IPs — every URL comes back "Sign in to confirm you're not a
bot". Player-client impersonation does not help; a cookie jar from a signed-in browser does.

1. In a browser logged into YouTube, install a `cookies.txt` extension (Get cookies.txt
   LOCALLY, or similar) and export for `youtube.com`.
2. Save it as `secrets/cookies.txt` in this repo.

That is all. `SIFT_COOKIES_PATH` already points at `/home/sift/secrets/cookies.txt`, the
directory is already mounted, and yt-dlp reads the jar on every call — so no rebuild and no
restart. Until the file exists, fetches simply run without cookies and the bot-challenge
error names this knob.

Everything in here except this README is gitignored. Never commit a cookie jar: it is a
live session token for your account.
