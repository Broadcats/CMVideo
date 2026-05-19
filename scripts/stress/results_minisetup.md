# Mini-app coverage stress test - 2026-05-18

Sites tested: **54**  ·  yt-dlp metadata: **22** (41%)  ·  fast-path mp4 win: **20** (37%)  ·  yt-dlp version: 2026.03.17

Tier breakdown: T0 3 · T1 10 · T2 7


| # | Site | yt-dlp | V/A | mp4 fast-path | mp3 (audio in slow-path?) | Note |
|---|------|--------|-----|---------------|---------------------------|------|
| 1 | 9GAG | FAIL | - | no | no | ERROR: [9gag] apAoYjm: Unable to download JSON metadata: HTTP Error 404: Not Found (caused |
| 2 | Aparat | FAIL | - | no | no | expected string or bytes-like object, got 'bool' |
| 3 | ArchiveOrg | OK | -- | tier 1 (direct) | no |  |
| 4 | Bandcamp-Track | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 5 | Beeg | FAIL | - | no | no | ERROR: [Beeg] 0983946056129650: Unable to download JSON metadata: HTTP Error 500: Internal |
| 6 | BigBuckBunny-MP4 | FAIL | - | no | no | ERROR: [generic] Unable to download webpage: HTTP Error 403: Forbidden (caused by <HTTPErr |
| 7 | BiliBili | OK | VA | tier 2 (ytdlp-pipe) | yes |  |
| 8 | BitChute | OK | -- | tier 1 (direct) | no |  |
| 9 | Bloomberg | FAIL | - | no | no | ERROR: [Bloomberg] apple-vision-pro-launch-strategy-video: Unable to download webpage: HTT |
| 10 | Coub | FAIL | - | no | no | ERROR: [Coub] 1gkb7y: Unable to download JSON metadata: HTTP Error 404: Not Found (caused  |
| 11 | Dailymotion | OK | VA | tier 2 (ytdlp-pipe) | yes |  |
| 12 | Direct-MP3 | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 13 | Direct-MP4-30s | OK | -- | tier 0 (direct) | no |  |
| 14 | Direct-MP4-5s | OK | -- | tier 0 (direct) | no |  |
| 15 | DW | FAIL | - | no | no | ERROR: Unsupported URL: https://www.dw.com/en/germany/s-1432 |
| 16 | Eporner | FAIL | - | no | no | ERROR: [Eporner] lkxcHcl22fL: Unable to extract hash; please report this issue on  https:/ |
| 17 | Imgur | FAIL | - | no | no | ERROR: [Imgur] 3jLn4l8: Unable to download JSON metadata: HTTP Error 403: Unknown Error (c |
| 18 | Instagram-Reel | FAIL | - | no | no | ERROR: [Instagram] CqkqMZJyHxK: Instagram sent an empty media response. Check if this post |
| 19 | KhanAcademy | FAIL | - | no | no | ERROR: [khanacademy] economics-finance-domain/macroeconomics/macro-basic-economic-concepts |
| 20 | Kick-VOD | FAIL | - | no | no | ERROR: [kick:vod] 4c5d4b5e-9f02-4d1d-aa3a-ec45f76e4da1: Unable to download JSON metadata:  |
| 21 | Mixcloud-Set | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 22 | Naver-TV | OK | -- | tier 1 (direct) | no |  |
| 23 | NBC-News | FAIL | - | no | no | list index out of range |
| 24 | Newgrounds | FAIL | - | no | no | ERROR: [Newgrounds] 717094: Unable to download webpage: HTTP Error 403: Forbidden (caused  |
| 25 | Niconico | OK | VA | tier 2 (ytdlp-pipe) | yes |  |
| 26 | PBS | FAIL | - | no | no | ERROR: An extractor error has occurred. (caused by KeyError('contentID')); please report t |
| 27 | Pornhub | FAIL | - | no | no | ERROR: [PornHub] ph5d4e6589c29ba: Unable to download webpage: HTTP Error 410: Gone (caused |
| 28 | Reddit-Video | FAIL | - | no | no | ERROR: [Reddit] 1cwx3p8: Unable to download JSON metadata: HTTP Error 404: Not Found (caus |
| 29 | RedTube | FAIL | - | no | no | ERROR: [RedTube] 41079841: Unable to extract video URL; please report this issue on  https |
| 30 | Rumble | FAIL | - | no | no | ERROR: [Rumble] v2xm2dk-rick-astley-rickroll.html: Unable to download webpage: HTTP Error  |
| 31 | SoundCloud-Pop | FAIL | - | no | no | ERROR: [soundcloud] Unable to download JSON metadata: HTTP Error 404: Not Found (caused by |
| 32 | SoundCloud-Track | OK | -A | tier 2 (ytdlp-pipe) | yes |  |
| 33 | SpankBang | FAIL | - | no | no | ERROR: [SpankBang] 4l0e4: Unable to download webpage: HTTP Error 404: Not Found (caused by |
| 34 | TED | OK | V- | tier 1 (direct) | no |  |
| 35 | TED-Embed | OK | V- | tier 1 (direct) | no |  |
| 36 | ThisVid-Embed | OK | -- | no | no |  |
| 37 | ThisVid-Watch | OK | -- | no | no |  |
| 38 | TikTok | FAIL | - | no | no | ERROR: [TikTok] 7232611617498910981: Your IP address is blocked from accessing this post |
| 39 | Twitch-Clip | FAIL | - | no | no | ERROR: [twitch:stream] clips: clips does not exist |
| 40 | Twitch-VOD | FAIL | - | no | no | ERROR: [twitch:vod] 2154723814: Video 2154723814 does not exist |
| 41 | Twitter-X | FAIL | - | no | no | ERROR: [twitter] 1785701540481503371: No video could be found in this tweet |
| 42 | TXXX | OK | -- | tier 1 (direct) | no |  |
| 43 | Veoh | FAIL | - | no | no | ERROR: [generic] Unable to download webpage: HTTPSConnection(host='www.veoh.com', port=443 |
| 44 | Vimeo-Player | OK | V- | tier 1 (direct) | no |  |
| 45 | Vimeo-StaffPick | OK | V- | tier 1 (direct) | no |  |
| 46 | W3Schools-MP4 | OK | -- | tier 0 (direct) | no |  |
| 47 | XHamster | FAIL | - | no | no | ERROR: [XHamster] 1509445: No video formats found!; please report this issue on  https://g |
| 48 | XNXX | OK | -- | tier 1 (direct) | no |  |
| 49 | XVideos | OK | -- | tier 1 (direct) | no |  |
| 50 | YouTube-Embed | FAIL | - | no | no | ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-brow |
| 51 | YouTube-Live | FAIL | - | no | no | ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-brow |
| 52 | YouTube-RickRoll | FAIL | - | no | no | ERROR: [youtube] dQw4w9WgXcQ: Please sign in. Use --cookies-from-browser or --cookies for  |
| 53 | YouTube-Short | FAIL | - | no | no | ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you’re not a bot. Use --cookies-from-brow |
| 54 | YouTube-Watch | FAIL | - | no | no | ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-brow |