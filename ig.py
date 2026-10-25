"""Instagram API (Instagram Login) client: media, insights, trial-reel publishing."""
import json
import os
import time

import requests

# Metrics we try to read for each reel. Unsupported ones are dropped automatically.
REEL_METRICS = [
    "views", "reach", "likes", "comments", "shares", "saved",
    "total_interactions", "ig_reels_avg_watch_time", "follows", "profile_visits",
]


class IGError(Exception):
    pass


class Instagram:
    def __init__(self, token, version="v23.0"):
        self.token = token
        self.version = version
        self.base = f"https://graph.instagram.com/{version}"
        self.supported_metrics = None
        self._user_id = None

    def _req(self, method, path, **params):
        url = path if path.startswith("http") else f"{self.base}/{path}"
        params["access_token"] = self.token
        for attempt in range(3):
            r = requests.request(method, url, params=params, timeout=60)
            if r.status_code >= 500 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            data = r.json() if r.content else {}
            if r.status_code >= 400 or "error" in data:
                raise IGError(f"{method} {path}: {data.get('error', data)}")
            return data

    # --- account / token ---

    def user_id(self):
        if not self._user_id:
            self._user_id = self._req("GET", "me", fields="user_id,username")["user_id"]
        return self._user_id

    def refresh_token(self):
        """Extends a long-lived token by another 60 days. Returns the new token."""
        data = self._req("GET", "https://graph.instagram.com/refresh_access_token",
                         grant_type="ig_refresh_token")
        self.token = data["access_token"]
        return self.token

    # --- media ---

    def list_reels(self):
        fields = "id,caption,media_type,media_product_type,permalink,timestamp"
        data = self._req("GET", "me/media", fields=fields, limit=100)
        out = []
        while True:
            out += [m for m in data.get("data", []) if m.get("media_product_type") == "REELS"]
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                return out
            r = requests.get(nxt, timeout=60)
            data = r.json()

    def media_url(self, media_id):
        """Fresh download URL of a reel (Instagram's URLs expire, so fetch right before use)."""
        return self._req("GET", media_id, fields="media_url").get("media_url")

    def insights(self, media_id):
        metrics = self.supported_metrics or REEL_METRICS
        try:
            data = self._req("GET", f"{media_id}/insights", metric=",".join(metrics))
        except IGError:
            if self.supported_metrics is not None:
                raise
            # Find which metrics this account/API version accepts, once.
            ok = []
            for m in REEL_METRICS:
                try:
                    self._req("GET", f"{media_id}/insights", metric=m)
                    ok.append(m)
                except IGError:
                    pass
            if not ok:
                raise
            self.supported_metrics = ok
            data = self._req("GET", f"{media_id}/insights", metric=",".join(ok))
        else:
            self.supported_metrics = metrics
        return {d["name"]: d["values"][0]["value"] for d in data.get("data", [])}

    # --- publishing ---

    def publish_trial_reel(self, video_path, caption, graduation="SS_PERFORMANCE"):
        """Uploads a local file as a Trial Reel (shown to non-followers first).

        graduation: SS_PERFORMANCE = Instagram shares it with followers automatically if it
        performs well; MANUAL = stays a trial.
        """
        uid = self.user_id()
        container = self._req(
            "POST", f"{uid}/media",
            media_type="REELS", upload_type="resumable", caption=caption,
            trial_params=json.dumps({"graduation_strategy": graduation}),
        )
        cid = container["id"]
        upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{cid}"
        size = os.path.getsize(video_path)
        with open(video_path, "rb") as f:
            r = requests.post(upload_uri, data=f, timeout=600, headers={
                "Authorization": f"OAuth {self.token}",
                "offset": "0",
                "file_size": str(size),
            })
        if r.status_code >= 400:
            raise IGError(f"upload failed: {r.text}")

        for _ in range(60):  # up to ~10 minutes of processing
            status = self._req("GET", cid, fields="status_code,status")
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise IGError(f"processing failed: {status}")
            time.sleep(10)
        else:
            raise IGError("processing timed out")

        return self._req("POST", f"{uid}/media_publish", creation_id=cid)["id"]
