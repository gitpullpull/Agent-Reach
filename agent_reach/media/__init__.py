# -*- coding: utf-8 -*-
"""The representation ladder — media as text, for every site yt-dlp reaches.

A shared capability, deliberately not registered in ``agent_reach.channels``:
channels are per-platform and answer "how do I authenticate and fetch text
here"; the ladder is orthogonal and answers "how far down into the media do I
go". One ladder serves every extractor, which is why this is a re-routing of
what upstream already installs rather than a new feature per site.

    tier 0    metadata, chapters, duration, subtitle availability   no download
    tier 0.5  one thumbnail                                          no download
    tier 1    timestamped subtitles                                  no download
    tier 1.5  comments                                               no download
    tier 2    frames from a named interval                           partial
    tier 3    frames from the whole video                            full

Tiers 0 through 1.5 are plain yt-dlp invocations an agent runs itself; they
are documented in ``skill/references/media-ladder.md`` rather than wrapped,
because upstream is an enabling layer and not a wrapper. This package holds
only what an agent cannot express as one shell command: sending pixels to a
vision model, and serialising downloads so the ladder does not burn a session.

Nothing here decides *whether* to descend. That is the agent's call.
"""

__all__ = ["cache", "extract", "frames", "ladder", "pace", "scratch"]
