Spotify-In-Playlist-Ranking

Ranking songs inside a playlist by ranking small subsections at a time, until a full ranking of the playlist exists — one that's remembered per-song, so it carries over to any other playlist sharing those songs.

How it works

nextSubsectionToSort (in algo.py) Given a list of already-sorted subsections and a larger unsorted collection, returns the next subsection worth asking the user to sort. While any songs in the collection have never appeared in *any* previously sorted subsection at all, it favors a random sample of those over its usual "in-between" pick — otherwise the in-between logic, which reasons from known relations, tends to keep recombining songs that have already been touched. Once every song has been seen at least once (or if there aren't enough never-ranked songs left to fill the subsection on their own), it falls back to — or tops the group up with — the informative "in-between" pick: the group that maximizes still-unknown pairwise relationships, so a newly-surfaced song still gets paired against something useful rather than sitting in an undersized group.

Completion detection (is_fully_determined / finalize_order) Pairwise relationships are transitively closed: if A was ranked before B in one subsection, and B before C in a completely different one (even from a different playlist), then A before C is already known, without ever comparing A and C directly. Once every pair in a playlist is comparable this way, finalize_order returns the full resulting order and there's nothing left to ask. This means a playlist can occasionally come up already fully ranked the first time you open it, purely from subsections built while ranking other playlists.

If two pieces of data genuinely conflict (e.g. a three-way cycle pieced together from separate sessions), the most recent judgment wins and the oldest contradicting one is set aside — the same rule already used when a pair is directly re-sorted later.

Local Spotify Ranker (the app)

Run it with:

bash
python app.py

No external Python packages are required — everything is stdlib (http.server, urllib), no Flask/spotipy/etc.

Open http://127.0.0.1:5000, paste your Spotify app client ID, connect the account, and choose a playlist. Use http://127.0.0.1:5000/callback as the redirect URI in your Spotify app settings.

Ranking a playlist

From the home page, pick a playlist and click Start ranking. You'll see a handful of cards (5 by default — pass ?size=N on /rank to change it for the session) that you can drag to reorder into your preferred ranking. Each card has a play button: while ranking or viewing the finished order, it starts playback in the context of the playlist you're currently working on (via the Spotify Web API), taking over your active device immediately — you don't need to pause whatever's already playing first. From the rankings gallery (not tied to a playlist) it just plays the track directly. If that API call can't succeed — no active Spotify Connect device, the connected account lacks the playback scope, or anything else goes wrong — it falls back to the previous behavior of opening a `spotify:track:` link in the Spotify app. Controlling playback needs the `user-modify-playback-state` / `user-read-playback-state` scopes; if you connected before this feature was added, reconnect once to grant them (see "Importing as a playlist" below for the same reconnect pattern). Hit Next to save that order and move to the next subsection — this repeats until the playlist is fully determined, at which point you get a scrollable, finished view of every song in order, with a button to import it as a new Spotify playlist.

Rankings are keyed by song (Spotify track ID), never by playlist, so a ranking made while sorting one playlist is automatically reused for any other playlist that shares those songs — including possibly finishing a playlist instantly on first load.

Rankings gallery

The Rankings link in the nav (present on every page) shows every subsection you've ever ranked as a small square with a preview of its top songs. Click one to open it in the same draggable card view, with Save / Cancel instead of Next — edits here affect every playlist that shares those songs going forward.

Data & persistence

Song metadata and the list of ranked subsections live in ranker_data.json, created next to app.py on first use (override the path with the RANKER_DATA_FILE environment variable). This is what survives restarts and reloads — nothing here depends on browser storage. The "Reset local state" button on the home page only clears the Spotify connection/session, not your saved rankings.

Spotify API caching

The playlist list and each playlist's tracks are cached in memory for 60 seconds by default (override with the SPOTIFY_CACHE_TTL_SECONDS environment variable), so normal navigation and page reloads don't re-hit the Spotify API and risk rate limiting (HTTP 429). A failed fetch is never cached, so a transient error just gets retried on the next load rather than being "stuck" for the TTL. Both the home page and the ranking page have a "refresh from Spotify" link (?refresh=1) to bypass the cache on demand — e.g. right after you've edited the playlist in Spotify itself. This cache is purely for Spotify API responses and has no effect on saved rankings, which are only ever read from/written to ranker_data.json.

Importing as a playlist

Importing requires the playlist-modify-private / playlist-modify-public Spotify scopes. If you connected before this feature was added, reconnect Spotify once (via the button that appears when needed) to grant it — Spotify doesn't let an existing connection pick up new permissions without re-authorizing.

Known limitations
Drag-and-drop uses the native HTML5 drag API — desktop mouse only, no touch support.
Local files and unavailable/removed tracks in a playlist are skipped (they don't have a stable Spotify track ID to rank by).
This is a single-user local tool: no accounts, no multi-device sync beyond the one ranker_data.json file.