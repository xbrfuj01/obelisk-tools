import json
import os
import subprocess

# Filesystem facts about the temp file we wrote on our own server (its
# name, path, timestamps from the moment it landed on disk) - not metadata
# the file actually carries, and showing them would just be misleading
# (e.g. "FileModifyDate: <today>" implies something false about the
# original file's history).
EXCLUDED_TAGS = {
    "SourceFile", "Directory", "FileName", "FileSize", "FilePermissions",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "ExifToolVersion", "Warning", "Error",
}

# Always shown, present or not - the common fields people actually care
# about (device/authorship/location/dates), so a "clean" file still shows
# the full shape of what could be there instead of just an empty list.
# Matched by base tag name only (not group), since ExifTool's exact group1
# label for a given tag can vary by file format. Anything ExifTool finds
# beyond this list (exotic MakerNotes, C2PA/JUMBF fields, unusual XMP
# namespaces, ...) is still included - this list only controls what gets
# force-shown as absent, never what gets hidden.
MASTER_TAGS = [
    "Make", "Model", "Software", "LensModel", "SerialNumber",
    "DateTimeOriginal", "CreateDate", "ModifyDate",
    "ExposureTime", "FNumber", "ISO", "FocalLength", "Flash", "WhiteBalance",
    "ImageWidth", "ImageHeight", "Orientation", "ColorSpace",
    "GPSLatitude", "GPSLongitude", "GPSAltitude", "GPSDateTime",
    "Artist", "Creator", "Copyright", "CopyrightNotice", "Rights", "By-line",
    "ImageDescription", "Description", "Title", "Caption-Abstract",
    "Keywords", "Subject", "UserComment",
    "Author", "Producer", "Company",
]


def read_metadata(path):
    """Returns a {"Group:Tag": value|None} dict covering every metadata
    field ExifTool actually found (EXIF, XMP, IPTC, ICC, maker notes,
    C2PA/JUMBF content credentials where the installed ExifTool version
    supports them, ...) plus every MASTER_TAGS field that wasn't found,
    with a value of None so the caller can render it as absent rather than
    just omitting it. Returns None if the file can't be read at all."""
    try:
        result = subprocess.run(
            # -u: also show "Unknown" tags - without it, ExifTool hides the
            # low-level structural fields (Item0, Pad, Hash, Alg, ...) that
            # C2PA/JUMBF content-credential manifests are actually made of,
            # so a file could carry a full C2PA manifest and this tool
            # would report zero AI-related metadata just because ExifTool
            # never surfaced it in the first place.
            # -b: return actual binary values (base64-encoded in JSON) -
            # e.g. C2PA hashes/signatures, embedded thumbnails, ICC
            # profiles - instead of a "(Binary data N bytes...)" stub.
            ["exiftool", "-j", "-G1", "-a", "-u", "-b", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not data:
        return None

    # Keep every distinct "Group:Tag" entry ExifTool returned, unfiltered -
    # a tag name can legitimately appear under more than one group (e.g.
    # "Keywords" in both IPTC and XMP-dc, with different values), and
    # collapsing those down to one per base name would silently drop real,
    # distinct metadata instead of showing all of it.
    result_tags = {}
    present_base_names = set()
    for key, value in data[0].items():
        base_key = key.split(":", 1)[-1]
        if base_key in EXCLUDED_TAGS:
            continue
        result_tags[key] = value
        present_base_names.add(base_key)

    for tag_name in MASTER_TAGS:
        if tag_name not in present_base_names:
            result_tags.setdefault(tag_name, None)

    return result_tags


# Group-name substrings that mean "this entire group is a C2PA/content-
# credentials manifest" - the single most reliable signal, since a real
# C2PA manifest is dozens of low-level fields (Item0, Pad, Hash, Alg,
# ExclusionsLength, ...) that don't individually look AI-related by name
# or value at all. Checked against the ExifTool -G1 group label.
AI_GROUP_KEYWORDS = ("c2pa", "jumbf", "jumd", "cbor")

# Tag-name and value substrings (all lowercase) that flag a still-present,
# non-removable field as coming from an AI generator/editor's provenance
# metadata specifically, rather than just an ordinary structural field
# ExifTool can't touch. Not exhaustive - new tools and tag conventions show
# up constantly - just the well-known ones as of when this was written.
AI_TAG_KEYWORDS = (
    "c2pa", "jumbf", "digitalsourcetype", "claimgenerator", "manifeststore",
    "aigenerated", "syntheticmedia", "trainedalgorithmic",
)
AI_VALUE_KEYWORDS = (
    "synthid", "midjourney", "dall-e", "dalle", "stable diffusion",
    "firefly", "gemini", "imagen", "runway", "leonardo.ai", "openai",
    "made with ai", "trainedalgorithmicmedia", "google ai", "generative ai",
    "ai-generated", "compositewithtrainedalgorithmicmedia", "c2pa", "jumbf",
)


def _looks_ai_related(full_key, value):
    group, _, tag_name = full_key.partition(":")
    if not tag_name:
        tag_name, group = group, ""
    lgroup = group.lower()
    ltag = tag_name.lower()
    if any(k in lgroup for k in AI_GROUP_KEYWORDS):
        return True
    if any(k in ltag for k in AI_TAG_KEYWORDS):
        return True
    if value:
        lvalue = str(value).lower()
        if any(k in lvalue for k in AI_VALUE_KEYWORDS):
            return True
    return False


def classify_metadata(before, after, verified=True):
    """Classifies each field from a before-strip read_metadata() result
    against an after-strip one (read from the cleaned output file) as:
      - "removable": had a value before, gone after - genuinely stripped.
      - "protected_ai": had a value before, still has one after (or removal
        couldn't be verified - see `verified`), AND looks like AI-
        generator/editor provenance metadata (C2PA, DigitalSourceType, a
        known AI tool name, ...) - present and stuck there.
      - "protected": had a value before AND still has one after, but isn't
        AI-related - ExifTool can't remove it for this format, or it's a
        derived/composite value (e.g. image dimensions) recomputed
        regardless of what gets stripped.
      - "absent": no value either way.
    Matched by base tag name (ignoring group), since ExifTool's group1
    label for the same tag can occasionally differ between two runs.

    `verified` must be False when the after-strip read itself failed
    (read_metadata returned None) rather than genuinely finding nothing -
    otherwise every single field would be misreported as "removable" just
    because we have no after-data to compare against, which is a false
    "yes it's gone" claim for a tool whose entire point is that claim."""
    after_by_base = {}
    for key, value in (after or {}).items():
        if value not in (None, ""):
            after_by_base[key.split(":", 1)[-1]] = value

    classified = {}
    for key, before_value in before.items():
        base_key = key.split(":", 1)[-1]
        if before_value in (None, ""):
            status = "absent"
        elif not verified or base_key in after_by_base:
            status = "protected_ai" if _looks_ai_related(key, before_value) else "protected"
        else:
            status = "removable"
        classified[key] = {"value": before_value, "status": status}
    return classified


def strip_metadata(input_path, output_path):
    """Writes a copy of input_path to output_path with every metadata tag
    ExifTool knows how to remove stripped out. Returns (True, None) on
    success or (False, error_text) if this format isn't writable."""
    try:
        result = subprocess.run(
            [
                "exiftool", "-all=",
                # -all= clears tags at ExifTool's parsed/interpreted level,
                # but C2PA/JUMBF manifests (Google/OpenAI provenance,
                # SynthID declarations, ...) live in a raw JPEG APP11
                # segment that -all= alone was verified (against a real
                # third-party C2PA validator) to leave physically intact -
                # ExifTool just stops being able to re-parse it afterward,
                # which made our own before/after check falsely report it
                # as removed. Deleting the APP11 segment directly forces
                # the actual bytes out. Harmless no-op on formats/files
                # that don't have one.
                "-APP11=",
                "-o", output_path, input_path,
            ],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "Перевищено час очікування"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if result.returncode != 0 or not os.path.exists(output_path):
        return False, (result.stderr or "").strip()[:500]
    return True, None
