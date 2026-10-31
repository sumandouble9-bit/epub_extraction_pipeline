import sys
from converter import main


args = ["convert", "--images"] + sys.argv[1:]
sys.exit(main(args))