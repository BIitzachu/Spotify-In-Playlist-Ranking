# Spotify-In-Playlist-Ranking
Ranking Songs inside of a playlist by ranking subsections of the playlist until only 1 possible ranking exists

I want once a playlist is selected to clear the page of everything except for a subsection size amount (default of 5) of hoverable and draggable cards that each represent one song. Each card will have a play button that opens the spotify app and will play the song. Along with the title and artist. The cards will scale so that there is no scrolling required. Then there is a next button in the bottom right that will save those rankings in some sort of way so even after reload these persist.  These should not be based on playlists at all only based on the songs so they can be used for other playlists too. Then it adds that subsection ranking to the list. And that list is passed to the next subsection to sort algo. Until there is nothing more that's possible to be given to rank. The finished sorted Playlist is then displayed in a scrollable format this time but similar cards. There should also be an import as playlist button that lets me import this as a new playlist. 
A seperate button on every screen should allow me to see a view with a whole bunch of squares that have mini versions of the ranking that they represent inside of them. When clicked those expand to show the card view described earlier. There is no next button here since these are not related to a specific playlist so the only buttons will be an cancel and a save button. These square that have the mini rankings are built from the list of subsections. These are the ones that are persistent. The subsections should be applicable to any playlist that is attempted to be sorted even if some of the songs in the subsections don't exist in the playlist that is currently being sorted. Update the algorithm so that this is possible. This should mean it could be possible to start the ranking of a playlist and it is already finished because of the subsections already having sorted those songs before previously. So make sure the logic accounts for that being possible.

If I want the play button in the rank playlist to play the song from inside the playlist that is currently being ranked and backup to the current implementation where it is playing the track itself. Also I want it to work if I am currently listening to a song too. Currently I need to pause my current song for this new song to play

nextSubsectionToSort function:
    Given a list of sorted arrays that are subsections of a larger unsorted dictionary, that unsorted dictionary, and the current set subsection size

    return the next subsection to sort in the unsorted dictionary.

    Eventually there should be a full ranking of everything in the dictionary possible. The subsection that is returned should be what would allow the dictionary to be fully sorted once the ranking of that subsection is known.

    The subsection that is returned should be the one that is most likely to be the next one to sort in order to achieve a fully sorted dictionary. 

    This subsection will then be sorted by the user and the ranking of that subsection will be used to update the unsorted dictionary. 

## Local Spotify picker

Run the local app with:

```bash
python app.py
```

Open `http://127.0.0.1:5000`, paste your Spotify app client ID, connect the account, and choose a playlist. Use `http://127.0.0.1:5000/callback` as the redirect URI in your Spotify app settings. No external Python packages are required.