"""Per-app hints that tell Claude where to look on screen. Add your own renderer here."""

PROFILES: dict[str, dict] = {
    "generic": {
        "label": "Other / auto-detect",
        "hint": "The renderer is unknown. Look for any progress bar, percentage, frame counter, "
                "elapsed/remaining time, or render log window.",
    },
    "blender": {
        "label": "Blender",
        "hint": "Blender renders in a 'Blender Render' window or the Image Editor. The header reads like "
                "'Frame:12 | Time:00:41.20 | Remaining:00:03.10 | Mem:... | Sample 64/128'. "
                "Command-line renders print 'Fra:12 Mem:... | Time:... | Remaining:...' in a console. "
                "A finished animation stops updating on the last frame of the range; 'Blender quit "
                "unexpectedly' or a crash window means a crash.",
    },
    "after_effects": {
        "label": "After Effects / Media Encoder",
        "hint": "After Effects shows a Render Queue panel with a progress bar, 'Elapsed' and 'Est. Remain' "
                "and a status column (Queued, Rendering, Done, Failed). Media Encoder shows a Queue panel "
                "with per-item progress. 'Failed', 'Render Error' or an error dialog means a failure.",
    },
    "premiere": {
        "label": "Premiere Pro",
        "hint": "Premiere exports show an 'Encoding' dialog or progress bar with percent and 'Estimated "
                "Time Remaining'. Error dialogs mention 'Error compiling movie' or similar.",
    },
    "resolve": {
        "label": "DaVinci Resolve",
        "hint": "Resolve's Deliver page shows a Render Queue with a progress bar, percent and 'Time "
                "remaining'. Completed jobs say 'Completed' with a duration; failures show a red error "
                "message or a 'Render Job Failed' dialog.",
    },
    "cinema4d": {
        "label": "Cinema 4D / Redshift",
        "hint": "Cinema 4D shows a Picture Viewer with frame progress in the status bar and a 'Render "
                "Queue' window. Redshift shows a Render View with progress and a log. Team Render / "
                "Commandline shows a console with frame numbers.",
    },
    "unreal": {
        "label": "Unreal Movie Render Queue",
        "hint": "Unreal's Movie Render Queue preview window shows 'Rendering Frame X/Y', percent "
                "complete, estimated time remaining and a progress bar. A 'Crash Reporter' window means "
                "the editor crashed.",
    },
}


def get(name: str) -> dict:
    return PROFILES.get(name) or PROFILES["generic"]
