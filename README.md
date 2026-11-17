# One Stop Netflix history

Lets the **One Stop Streaming card** show a Netflix profile's real *Recently played* row.

Netflix has no public API for watch history. This reads the same viewing-activity page netflix.com shows a signed-in member, using that member's own website sign-in. Netflix may change or block it at any time, and it may not be allowed by Netflix's terms of use. Use at your own risk.

## Set up
1. Install through HACS (custom repository, type *Integration*) and restart Home Assistant.
2. Settings → Devices & services → Add integration → **One Stop Netflix history**, or press **Connect Netflix** in the Streaming card editor.
3. Follow the steps on screen (copy two values from a signed-in browser). Add it once per Netflix profile you want to include.

If Netflix signs the connection out (password change, or every few months), Home Assistant shows a *Reconnect* notice.

## Service
`one_stop_netflix.get_history` (returns data): `limit`, `refresh`. Results are cached for 5 minutes.
