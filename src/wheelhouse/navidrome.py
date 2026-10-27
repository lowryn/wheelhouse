"""Subsonic API client for Navidrome."""
from __future__ import annotations

import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from typing import Any


class NavidromeClient:
    def __init__(self, url: str, user: str, password: str, client: str = "wheelhouse"):
        self._url = url.rstrip("/")
        self._user = user
        self._password = password
        self._client = client

    def _query(self, endpoint: str, **params: Any) -> str:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()
        base: dict[str, str] = {
            "u": self._user, "t": token, "s": salt,
            "v": "1.16.1", "c": self._client, "f": "json",
        }
        base.update({k: str(v) for k, v in params.items() if v is not None})
        return f"{self._url}/rest/{endpoint}.view?{urllib.parse.urlencode(base)}"

    def get(self, endpoint: str, **params: Any) -> dict:
        with urllib.request.urlopen(self._query(endpoint, **params), timeout=30) as r:
            body = json.load(r)["subsonic-response"]
        if body.get("status") != "ok":
            raise RuntimeError(f"{endpoint}: {body.get('error')}")
        return body

    def raw(self, endpoint: str, **params: Any) -> bytes:
        with urllib.request.urlopen(self._query(endpoint, **params), timeout=120) as r:
            return r.read()

    def all_songs(self, page_size: int = 500) -> list[dict]:
        songs: list[dict] = []
        offset = 0
        while True:
            batch = (
                self.get("search3", query="", songCount=page_size, songOffset=offset,
                         artistCount=0, albumCount=0)["searchResult3"].get("song", [])
            )
            songs.extend(batch)
            if len(batch) < page_size:
                return songs
            offset += page_size

    def playlists(self) -> list[dict]:
        return self.get("getPlaylists")["playlists"].get("playlist", [])

    def playlist_songs(self, playlist_id: str) -> list[dict]:
        return self.get("getPlaylist", id=playlist_id)["playlist"].get("entry", [])

    def album(self, album_id: str) -> dict:
        return self.get("getAlbum", id=album_id)["album"]

    def song(self, song_id: str) -> dict:
        return self.get("getSong", id=song_id)["song"]

    def cover_art(self, cover_id: str, size: int = 600) -> bytes:
        return self.raw("getCoverArt", id=cover_id, size=size)

    def download(self, song_id: str) -> bytes:
        return self.raw("download", id=song_id)

    def scrobble(self, song_id: str, time_ms: int) -> None:
        self.get("scrobble", id=song_id, time=time_ms, submission="true")

    def create_playlist(self, name: str, song_ids: list[str]) -> dict:
        return self.get("createPlaylist", name=name, songId=song_ids)["playlist"]
