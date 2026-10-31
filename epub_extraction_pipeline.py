"""Run EPUB extraction with default settings.

Usage:
    python run.py                     # convert all EPUBs in input/ with images
    python run.py book.epub           # convert a specific file
    python run.py --no-summary        # skip LLM summary generation
    python run.py --no-formulas       # skip formula detection
"""
import sys
from converter import main
# from config import ISBN_FILTER_EXCEL

# ISBN_FILTER_EXCEL = r"C:\Users\User\Downloads\Spanish Data_2000_ISBN.xlsx"

# args = ["convert", "--images", "--isbn-filter", ISBN_FILTER_EXCEL] + sys.argv[1:]
args = ["convert", "--images"] + sys.argv[1:]
sys.exit(main(args))