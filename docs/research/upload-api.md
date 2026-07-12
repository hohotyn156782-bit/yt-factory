All questions are now settled with primary sources. Writing the report.

# Automated YouTube Uploads for a Solo Creator — State as of July 2026

## 1. videos.insert private lock for unaudited projects — still in force

- **Still true in 2026.** The official `videos.insert` docs state: "All videos uploaded via the videos.insert endpoint from unverified API projects created after 28 July 2020 will be restricted to private viewing mode." To lift it, "each API project must undergo an audit." [https://developers.google.com/youtube/v3/docs/videos/insert](https://developers.google.com/youtube/v3/docs/videos/insert)
- **The owner CANNOT flip it in Studio — it is hard-locked, no appeal.** YouTube Help ("Videos locked as private"): "For videos that have been locked as private due to upload via an unverified API service, you will not be able to appeal. You'll need to re-upload the video via a verified API service or via the YouTube app/site." So passing the audit later does NOT retroactively unlock earlier uploads either — re-upload is required. [https://support.google.com/youtube/answer/7300965](https://support.google.com/youtube/answer/7300965)
- Community confirmation of the lock in practice: [https://github.com/porjo/youtubeuploader/issues/86](https://github.com/porjo/youtubeuploader/issues/86)

## 2. The audit process for personal automation

- One form covers both audit and quota extension: **YouTube API Services – Audit and Quota Extension Form**, [https://support.google.com/youtube/contact/yt_api_form](https://support.google.com/youtube/contact/yt_api_form). You can request an audit at the default quota just to lift the private lock. Requirements: clear use-case description, demo video of your OAuth flow, ToS agreement. [https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
- **Timeline: no guaranteed SLA.** 2026 guides report "plan for 2–4 weeks" ([https://postproxy.dev/blog/youtube-upload-api-guide/](https://postproxy.dev/blog/youtube-upload-api-guide/)); others report weeks to months, approval "not guaranteed and depends on a clear use case, a public privacy policy, terms of service" ([https://www.blotato.com/blog/youtube-api-pricing](https://www.blotato.com/blog/youtube-api-pricing)). Rejections can be appealed ([https://developers.google.com/youtube/terms/developer-policies-guide](https://developers.google.com/youtube/terms/developer-policies-guide)).
- For "single developer uploading to own channel" the use case is legitimate and small; the main practical risks are slow/no response and form answers that look like bulk automation. No official approval-rate data exists — treat approval as likely-but-not-certain and weeks-scale.

## 3. OAuth testing vs production — the refresh-token problem

- **Testing status = 7-day refresh-token death**, officially: an external app "with a publishing status of 'Testing' is issued a refresh token expiring in 7 days." [https://developers.google.com/identity/protocols/oauth2#expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- **The 2026 solo-dev fix: click "Publish app" to In production and simply stay unverified.** Sensitive scopes (`youtube.upload` is sensitive, not restricted) keep working; users see an "unverified app" warning at consent time and the app has a lifetime cap of 100 users — irrelevant when the only user is you. Refresh tokens in production have **no 7-day expiry** (they die only via 6-month inactivity, revocation, or the 100-tokens-per-client limit; regular pipeline use resets the inactivity clock). [https://support.google.com/cloud/answer/15549945](https://support.google.com/cloud/answer/15549945), [https://developers.google.com/identity/protocols/oauth2#expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- "Internal" mode avoids the warning entirely but requires Google Workspace — not applicable to a plain Gmail account.
- Key distinction people confuse: **OAuth consent-screen verification** (Trust & Safety) and the **YouTube API compliance audit** are separate processes. Publishing the OAuth app unverified fixes token expiry, but only the YouTube audit lifts the private lock.

## 4. Quota math — dramatically better since Dec 2025

- **The 1600-unit figure is obsolete.** Official revision history: Dec 4, 2025 — video upload cost cut "from approximately 1600 units to approximately 100 units"; June 1, 2026 — `videos.insert` and `search.list` moved to **their own quota buckets**. [https://developers.google.com/youtube/v3/revision_history](https://developers.google.com/youtube/v3/revision_history)
- Current defaults: `videos.insert` = **1 unit per call in a dedicated bucket of 100 calls/day**; `search.list` = own 100-call/day bucket; everything else shares **10,000 units/day**. [https://developers.google.com/youtube/v3/determine_quota_cost](https://developers.google.com/youtube/v3/determine_quota_cost), [https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
- Per fully-decorated upload: insert 1 (own bucket) + `thumbnails.set` 50 + `playlistItems.insert` 50 (+ `videos.update` 50 if needed) from the shared pool → **up to 100 uploads/day**, and ~66–100/day even with thumbnail + playlist on default quota. Any realistic solo cadence (1–10/day) uses under 2% of quota.
- Caveat: YouTube also enforces hidden **per-channel** daily upload caps independent of API quota (`uploadLimitExceeded` / 429 despite headroom): [https://github.com/googleapis/google-api-python-client/issues/2753](https://github.com/googleapis/google-api-python-client/issues/2753)

## 5. What automation setups actually work in 2026

| Path | Works for public videos? | Notes |
|---|---|---|
| Official API, **audited** project | Yes — fully automatic, `publishAt` works | The only true zero-touch official path |
| Official API, unaudited, "upload private then publish manually" | **No** — video is hard-locked private, owner can't flip it (see §1) | Common misconception; dead end |
| Unaudited + `publishAt` | **No** — publishAt is just a deferred public flip; the lock wins, video stays private ([https://developers.google.com/youtube/v3/docs/videos](https://developers.google.com/youtube/v3/docs/videos), §1 sources) | |
| Third-party scheduler with **their** audited API project (Metricool free: 50 posts/mo incl. YouTube; Buffer free; Postiz cloud) | Yes — ToS-safe, videos publish public | [https://help.metricool.com/en/article/schedule-and-publish-on-youtube-btmd1m/](https://help.metricool.com/en/article/schedule-and-publish-on-youtube-btmd1m/), [https://postiz.com/blog/free-social-media-scheduling-tools](https://postiz.com/blog/free-social-media-scheduling-tools) |
| Self-hosted Postiz / n8n with your own API keys | No advantage — uses YOUR unaudited project → same private lock | |
| Headless-browser upload (Selenium/Playwright) | Fragile, ToS-gray, breaks on UI changes; used by some repos ([https://github.com/ContentAutomation/YouTubeUploader](https://github.com/ContentAutomation/YouTubeUploader)) | Not recommended for a channel you care about |

## Recommended setup for your case

1. **Day 1:** Google Cloud project → enable YouTube Data API v3 → OAuth consent screen External → **publish to "In production", leave unverified** → do the OAuth dance once locally with `access_type=offline`, store the refresh token in GitHub Actions Secrets. Token now survives indefinitely while used.
2. **Day 1, in parallel:** submit the **Audit and Quota Extension Form** describing "personal automation uploading to my own channel, single user, default quota is sufficient" with a short demo video. This is the only thing that unlocks fully-automatic public uploads.
3. **Until the audit passes (weeks):** do NOT burn real content through your own unaudited project — those videos get hard-locked private and would need re-upload. Bridge with **Metricool's free tier** (50 posts/month via their audited API) driven from your pipeline, or accept manual publishing through Studio's normal upload.
4. **After audit approval:** pipeline calls `videos.insert` with `privacyStatus: private` + `publishAt` for scheduled public releases (or `public` directly), then `thumbnails.set` and `playlistItems.insert`. Default quota supports up to 100 uploads/day — no extension needed. Watch for the hidden per-channel `uploadLimitExceeded` cap if you exceed roughly 10–20 uploads/day.