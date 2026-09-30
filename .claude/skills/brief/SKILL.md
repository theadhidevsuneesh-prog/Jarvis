---
name: brief
description: JARVIS morning brief — date, weather, today's calendar, emails awaiting a reply, and open tasks, spoken aloud. Use when the user says good morning, asks for their brief, or asks what's on today.
---

Gather these, skipping any source that isn't available (don't dwell on what's missing):

1. **Date and time** — run `Get-Date -Format "dddd d MMMM, h:mm tt"` in PowerShell.
2. **Weather** — WebFetch `https://wttr.in/?format=%l:+%C,+%t,+feels+like+%f,+rain+%p` (location is detected automatically).
3. **Calendar** — if a Google Calendar tool is connected, list today's events.
4. **Email** — if a Gmail tool is connected, find unread or important emails from the last day that Adhidev hasn't replied to. Name the sender and the gist, at most five.
5. **Tasks** — read `tasks.md` in the jarvis folder; mention unchecked items. If Notion is connected and Adhidev keeps a to-do or tasks page there, include its open items too.

Then deliver the brief as JARVIS: one flowing spoken paragraph, under about 120 words, most important thing first.
No lists or headings (it is read aloud). If calendar or email aren't connected, add one short sentence at the end
suggesting connecting them at claude.ai → Settings → Connectors.
