Spotify-In-Playlist-Ranking

Ranking songs inside a playlist by ranking small subsections at a time, until a full ranking of the playlist exists — one that's remembered per-song, so it carries over to any other playlist sharing those songs.

How it works

nextSubsectionToSort (in algo.py) Given a list of already-sorted subsections and a larger unsorted collection, returns the next subsection worth asking the user to sort — the one that will teach the algorithm the most about the full ordering. It's biased toward subsections with mostly-unknown pairwise relationships, with a light preference for including one or two already-known items as anchors.

Completion detection (is_fully_determined / finalize_order) Pairwise relationships are transitively closed: if A was ranked before B in one subsection, and B before C in a completely different one (even from a different playlist), then A before C is already known, without ever comparing A and C directly. Once every pair in a playlist is comparable this way, finalize_order returns the full resulting order and there's nothing left to ask. This means a playlist can occasionally come up already fully ranked the first time you open it, purely from subsections built while ranking other playlists.

If two pieces of data genuinely conflict (e.g. a three-way cycle pieced together from separate sessions), the most recent judgment wins and the oldest contradicting one is set aside — the same rule already used when a pair is directly re-sorted later.

Local Spotify Ranker (the app)

Run it with:

bash
python app.py

No external Python packages are required — everything is stdlib (http.server, urllib), no Flask/spotipy/etc.

Open http://127.0.0.1:5000, paste your Spotify app client ID, connect the account, and choose a playlist. Use http://127.0.0.1:5000/callback as the redirect URI in your Spotify app settings.

Ranking a playlist

From the home page, pick a playlist and click Start ranking. You'll see a handful of cards (5 by default — pass ?size=N on /rank to change it for the session) that you can drag to reorder into your preferred ranking. Each card has a play button that opens the song directly in the Spotify app. Hit Next to save that order and move to the next subsection — this repeats until the playlist is fully determined, at which point you get a scrollable, finished view of every song in order, with a button to import it as a new Spotify playlist.

Rankings are keyed by song (Spotify track ID), never by playlist, so a ranking made while sorting one playlist is automatically reused for any other playlist that shares those songs — including possibly finishing a playlist instantly on first load.

Rankings gallery

The Rankings link in the nav (present on every page) shows every subsection you've ever ranked as a small square with a preview of its top songs. Click one to open it in the same draggable card view, with Save / Cancel instead of Next — edits here affect every playlist that shares those songs going forward.

Data & persistence

Song metadata and the list of ranked subsections live in ranker_data.json, created next to app.py on first use (override the path with the RANKER_DATA_FILE environment variable). This is what survives restarts and reloads — nothing here depends on browser storage. The "Reset local state" button on the home page only clears the Spotify connection/session, not your saved rankings.

Importing as a playlist

Importing requires the playlist-modify-private / playlist-modify-public Spotify scopes. If you connected before this feature was added, reconnect Spotify once (via the button that appears when needed) to grant it — Spotify doesn't let an existing connection pick up new permissions without re-authorizing.

Known limitations
Drag-and-drop uses the native HTML5 drag API — desktop mouse only, no touch support.
Local files and unavailable/removed tracks in a playlist are skipped (they don't have a stable Spotify track ID to rank by).
This is a single-user local tool: no accounts, no multi-device sync beyond the one ranker_data.json file.