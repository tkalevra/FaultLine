"""Build faultline.mcpb from the claude-desktop directory."""
import zipfile
import os

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, "faultline.mcpb")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.write(os.path.join(DIR, "manifest.json"), "manifest.json")
    zf.write(os.path.join(DIR, "server", "faultline_proxy.py"), "server/faultline_proxy.py")

print(f"Built: {OUT} ({os.path.getsize(OUT)} bytes)")
