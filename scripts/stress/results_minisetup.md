# Mini-app coverage stress test - 2026-05-18

Sites tested: **59**  ·  yt-dlp metadata: **26** (44%)  ·  fast-path mp4 win: **22** (37%)  ·  yt-dlp version: 2026.03.17

Tier breakdown: T0 5 · T1 10 · T2 7


| # | Site | yt-dlp | V/A | mp4 fast-path | mp3 (audio in slow-path?) | Note |
|---|------|--------|-----|---------------|---------------------------|------|
| 1 | 9GAG | FAIL | - | no | no | ERROR: [9gag] aP1q6m1: Unable to download JSON metadata: HTTP Error 404: Not Found (caused |
| 2 | Aparat | FAIL | - | no | no | expected string or bytes-like object, got 'bool' |
| 3 | ArchiveOrg | OK | -- | tier 1 (direct) | no |  |
| 4 | Bandcamp-Album | OK | -- | no | no |  |
| 5 | Bandcamp-Track | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 6 | Beeg | FAIL | - | no | no | ERROR: [Beeg] 0983946056129650: Unable to download JSON metadata: HTTP Error 500: Internal |
| 7 | BiliBili | FAIL | - | no | no | ERROR: [BiliBili] 1uT4y1P7CX: This video may be deleted or geo-restricted. You might want  |
| 8 | BitChute | OK | -- | tier 1 (direct) | no |  |
| 9 | Bloomberg | FAIL | - | no | no | ERROR: [Bloomberg] apple-vision-pro-launch-strategy-video: Unable to download webpage: HTT |
| 10 | Coub | FAIL | - | no | no | ERROR: [Coub] 3rwa3a: Unable to download JSON metadata: HTTP Error 404: Not Found (caused  |
| 11 | Dailymotion | OK | VA | tier 2 (ytdlp-pipe) | yes |  |
| 12 | Direct-MP3-15s | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 13 | Direct-MP3-3s | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 14 | Direct-MP4-15s | OK | -- | tier 0 (direct) | no |  |
| 15 | Direct-MP4-30s | OK | -- | tier 0 (direct) | no |  |
| 16 | Direct-MP4-5s | OK | -- | tier 0 (direct) | no |  |
| 17 | DW | FAIL | - | no | no | ERROR: Unsupported URL: https://www.dw.com/en/germany/s-1432 |
| 18 | Eporner | FAIL | - | no | no | ERROR: [Eporner] EpoRnEr: Unable to extract hash; please report this issue on  https://git |
| 19 | Imgur | FAIL | - | no | no | ERROR: [Imgur] 3jLn4l8: Unable to download JSON metadata: HTTP Error 403: Unknown Error (c |
| 20 | Instagram-Reel | FAIL | - | no | no | ERROR: [Instagram] CqkqMZJyHxK: Instagram sent an empty media response. Check if this post |
| 21 | KhanAcademy | FAIL | - | no | no | ERROR: [khanacademy] economics-finance-domain/macroeconomics/macro-basic-economic-concepts |
| 22 | Kick-VOD | FAIL | - | no | no | ERROR: [kick:vod] 4c5d4b5e-9f02-4d1d-aa3a-ec45f76e4da1: Unable to download JSON metadata:  |
| 23 | LBC | FAIL | - | no | no | ERROR: Unsupported URL: https://www.lbc.co.uk/radio/presenters/nick-ferrari/ |
| 24 | LearningContainer-MP4 | OK | -- | tier 0 (direct) | no |  |
| 25 | Mixcloud-Set | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 26 | Naver-TV | OK | -- | tier 1 (direct) | no |  |
| 27 | NBC-News | FAIL | - | no | no | list index out of range |
| 28 | Newgrounds | FAIL | - | no | no | ERROR: [Newgrounds] 717094: Unable to download webpage: HTTP Error 403: Forbidden (caused  |
| 29 | NHK | OK | -- | no | no |  |
| 30 | Niconico | OK | VA | tier 2 (ytdlp-pipe) | yes |  |
| 31 | PBS | FAIL | - | no | no | ERROR: An extractor error has occurred. (caused by KeyError('contentID')); please report t |
| 32 | Pornhub | FAIL | - | no | no | ERROR: [PornHub] ph5d4e6589c29ba: Unable to download webpage: HTTP Error 410: Gone (caused |
| 33 | Reddit-Video | FAIL | - | no | no | ERROR: [Reddit] k1xa8m: Unable to download JSON metadata: HTTP Error 404: Not Found (cause |
| 34 | RedTube | FAIL | - | no | no | ERROR: [RedTube] 38864951: Unable to extract video URL; please report this issue on  https |
| 35 | Rumble | FAIL | - | no | no | ERROR: [Rumble] v6oka5o-elon-musk-was-supposed-to-stop-this.html: Unable to download webpa |
| 36 | SampleVideos-MP4 | FAIL | - | no | no | ERROR: [generic] Unable to download webpage: (<HTTPSConnection(host='www.sample-videos.com |
| 37 | SoundCloud-Pop | FAIL | - | no | no | ERROR: [soundcloud] Unable to download JSON metadata: HTTP Error 404: Not Found (caused by |
| 38 | SoundCloud-Track | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 39 | SpankBang | FAIL | - | no | no | ERROR: [SpankBang] 56b3d: Unable to download webpage: HTTP Error 404: Not Found (caused by |
| 40 | TED | OK | V- | tier 1 (direct) | no |  |
| 41 | TED-Embed | OK | V- | tier 1 (direct) | no |  |
| 42 | ThisVid-Canon | OK | -- | no | no |  |
| 43 | ThisVid-Embed | OK | -- | no | no |  |
| 44 | TikTok-Verified | FAIL | - | no | no | ERROR: [TikTok] 7232611617498910981: Your IP address is blocked from accessing this post |
| 45 | Twitch-Clip | FAIL | - | no | no | ERROR: [twitch:clips] SmellyFamousFalconKappaWealth-9DiZdK4iIXHzx9SR: This clip is no long |
| 46 | Twitch-VOD | FAIL | - | no | no | ERROR: [twitch:vod] 2154723814: Video 2154723814 does not exist |
| 47 | Twitter-X | FAIL | - | no | no | ERROR: [twitter] 1722127666497786143: No video could be found in this tweet |
| 48 | TXXX | OK | -- | tier 1 (direct) | no |  |
| 49 | Veoh | FAIL | - | no | no | ERROR: [generic] Unable to download webpage: HTTPSConnection(host='www.veoh.com', port=443 |
| 50 | Vimeo-Player | OK | V- | tier 1 (direct) | no |  |
| 51 | Vimeo-StaffPick | OK | V- | tier 1 (direct) | no |  |
| 52 | W3Schools-MP4 | OK | -- | tier 0 (direct) | no |  |
| 53 | XHamster | FAIL | - | no | no | ERROR: [XHamster] 1509445: No video formats found!; please report this issue on  https://g |
| 54 | XNXX | OK | -- | tier 1 (direct) | no |  |
| 55 | XVideos | OK | -- | tier 1 (direct) | no |  |
| 56 | YouTube-Embed | FAIL | - | no | no | ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-brow |
| 57 | YouTube-FirstVideo | FAIL | - | no | no | ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-brow |
| 58 | YouTube-RickRoll | FAIL | - | no | no | ERROR: [youtube] dQw4w9WgXcQ: Please sign in. Use --cookies-from-browser or --cookies for  |
| 59 | YouTube-Short | FAIL | - | no | no | ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you’re not a bot. Use --cookies-from-brow |