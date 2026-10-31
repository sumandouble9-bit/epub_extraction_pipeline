"""Scan extracted EPUB output folders and report which have an images subfolder.

Usage:
    python find_image_folders.py
"""
import os

from config import DEFAULT_OUTPUT_DIR
# OUTPUT_DIR = r"\\192.168.4.28\Common\Suman Kumar\extracted_epub_josn_files"
OUTPUT_DIR = DEFAULT_OUTPUT_DIR
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image_folders_report.log")

with_images = []
without_images = []

for name in sorted(os.listdir(OUTPUT_DIR)):
    folder = os.path.join(OUTPUT_DIR, name)
    if not os.path.isdir(folder):
        continue
    img_dir = os.path.join(folder, "images")
    if os.path.isdir(img_dir):
        count = len([f for f in os.listdir(img_dir) if os.path.isfile(os.path.join(img_dir, f))])
        with_images.append((name, count))
    else:
        without_images.append(name)

lines = []
lines.append(f"Books WITH images folder: {len(with_images)}")
lines.append("-" * 50)
for isbn, count in with_images:
    lines.append(f"  {isbn}  ({count} files)")

lines.append(f"\nBooks WITHOUT images folder: {len(without_images)}")
lines.append("-" * 50)
for isbn in without_images:
    lines.append(f"  {isbn}")

lines.append(f"\nTotal: {len(with_images) + len(without_images)} books")

report = "\n".join(lines)
print(report)

with open(LOG_FILE, "w", encoding="utf-8") as f:
    f.write(report + "\n")
print(f"\nLog saved: {LOG_FILE}")
