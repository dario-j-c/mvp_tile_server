# Introduction

This is a MVP to ensure we can serve tiles to another app locally.

In the future, another methodology may be used (for example serving MBTiles, Cloud Optimised GeoTiffs), but this serves the purpose of allowing work to be done while this occurs.

# Usage

To setup the simple servet to serve the files, the following must be run:

```bash
python3 main.py [path to tiles] -p [port to use] -b [address to bind]
```

- The path defaults to the current path
- The port default to 8000
- The address defaults to 0.0.0.0.


The file structure mimics the urls layout of {z}/{x}/{y}.
