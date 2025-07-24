#!/usr/bin/env python3
"""
Simple HTTP server for serving files from a specified directory.
Provides file listing, restricts access to the specified directory,
and includes CORS support and basic caching for static assets (like tiles).
"""

import argparse
import email.utils  # For formatting HTTP dates
import io
import mimetypes
import os
import socket
import sys
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

# List of file extensions to exclude from directory listings
# Add or remove extensions as needed (include the dot)
EXCLUDED_EXTENSIONS = [
    ".py",  # Python source files
    ".pyc",  # Python compiled files
    ".DS_Store",  # macOS system files
]

# List of directory names to exclude from directory listings (e.g., virtual environments)
EXCLUDED_DIRECTORIES = [
    ".git",  # Git repository directory
    ".venv",  # Python virtual environment
    "venv",  # Another common Python virtual environment name
    "node_modules",  # JavaScript dependencies
    "data",  # Arbitrary files
]


class RestrictedCORSAndCacheFileHandler(SimpleHTTPRequestHandler):
    """
    Custom HTTP request handler that:
    1. Only serves files from the specified base directory.
    2. Sanitizes file paths to prevent directory traversal.
    3. Generates a directory listing if requested, excluding specified file types and directories.
    4. Adds CORS (Access-Control-Allow-Origin: *) header.
    5. Adds basic caching headers for static assets (like tiles).
    """

    def __init__(self, *args, **kwargs):
        # The 'directory' is passed via partial in run_server.
        # We pop it from kwargs to ensure it's handled explicitly and not passed twice
        # to the super().__init__ call.
        self.directory = kwargs.pop("directory")
        # Ensure the base directory is an absolute path for security and consistency.
        self.directory = os.path.abspath(self.directory)

        # Register custom MIME types before the super().__init__ call to ensure they are available
        # when SimpleHTTPRequestHandler tries to guess types.
        self.add_extra_mimetypes()

        # Call the parent class's __init__ method, passing the required arguments.
        # We explicitly pass our 'directory' as the directory for the parent handler to use.
        super().__init__(
            *args,
            directory=str(self.directory),  # Ensure directory is passed as a string
            **kwargs,
        )

    def add_extra_mimetypes(self):
        """Adds custom or commonly needed MIME types for serving, especially for map tiles."""
        # Map Tile specific MIME types (critical for browsers to correctly interpret tiles)
        mimetypes.add_type("image/png", ".png")
        mimetypes.add_type("image/jpeg", ".jpeg")
        mimetypes.add_type("image/jpeg", ".jpg")
        mimetypes.add_type("image/webp", ".webp")
        mimetypes.add_type("image/gif", ".gif")
        mimetypes.add_type("image/tiff", ".tif")
        mimetypes.add_type("image/tiff", ".tiff")

        # Vector Tiles (PBF) - essential for vector tile serving
        mimetypes.add_type("application/x-protobuf", ".pbf")
        mimetypes.add_type("application/vnd.mapbox-vector-tile", ".mvt")

        # Other common types (explicitly adding them to ensure consistency)
        mimetypes.add_type("application/json", ".json")
        mimetypes.add_type("text/css", ".css")
        mimetypes.add_type("text/javascript", ".js")
        mimetypes.add_type("text/html", ".html")
        mimetypes.add_type("text/xml", ".xml")
        mimetypes.add_type("application/geo+json", ".geojson")
        mimetypes.add_type("text/markdown", ".md")
        mimetypes.add_type("text/markdown", ".markdn")
        mimetypes.add_type("text/markdown", ".markdown")
        mimetypes.add_type("text/markdown", ".mdown")

    def translate_path(self, path):
        """
        Translate URL path to filesystem path, ensuring we stay strictly within
        the specified base directory (`self.directory`).
        This method is critical for preventing directory traversal attacks.
        It also correctly decodes URL-encoded characters.
        """
        # Remove query parameters and anchors
        path = path.split("?", 1)[0]
        path = path.split("#", 1)[0]

        # Decode URL-encoded characters and remove leading slashes
        path = unquote(path).lstrip("/")

        # Construct the full filesystem path
        full_path = os.path.normpath(os.path.join(self.directory, path))

        # Security check: Ensure the resolved path is indeed inside the base directory.
        # This prevents '..' (directory traversal) attempts.
        if not full_path.startswith(self.directory):
            self.log_message(
                f"Attempted path traversal detected: {path} -> {full_path}"
            )
            return None  # Indicate an invalid path

        return full_path

    def do_GET(self):
        """
        Handles GET requests.
        Custom logic to prioritize directory listings over automatic index.html serving
        when a directory is requested. Otherwise, delegates to the parent class for file serving.
        """
        # Translate the requested URL path to a local filesystem path.
        path_on_disk = self.translate_path(self.path)

        # If translation failed (e.g., path traversal detected), send 403 Forbidden.
        if path_on_disk is None:
            self.send_error(403, "Forbidden: Directory traversal attempt blocked.")
            return

        # If the resolved path points to a directory on disk:
        if os.path.isdir(path_on_disk):
            # If the URL for a directory does not end with a slash (e.g., "/my_dir" instead of "/my_dir/"),
            # send a 301 redirect to add the trailing slash. This is standard HTTP practice.
            if not self.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", self.path + "/")
                self.end_headers()
                return

            # If it's a directory request with a trailing slash, generate and serve the directory listing.
            file_to_serve = self.list_directory(path_on_disk)
            if file_to_serve:
                try:
                    self.copyfile(file_to_serve, self.wfile)
                finally:
                    file_to_serve.close()
            return  # Request handled

        # If it's not a directory (meaning it's a file request),
        # delegate to the parent SimpleHTTPRequestHandler's do_GET method.
        # Our overridden translate_path will ensure it operates within the specified directory.
        super().do_GET()

    def do_HEAD(self):
        """
        Handles HEAD requests.
        Similar to do_GET, prioritizes directory listing headers over automatic index.html.
        """
        # Translate the requested URL path to a local filesystem path.
        path_on_disk = self.translate_path(self.path)

        # If translation failed (e.g., path traversal detected), send 403 Forbidden.
        if path_on_disk is None:
            self.send_error(403, "Forbidden: Directory traversal attempt blocked.")
            return

        # If the resolved path points to a directory on disk:
        if os.path.isdir(path_on_disk):
            # If the URL for a directory does not end with a slash, redirect.
            if not self.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", self.path + "/")
                self.end_headers()
                return

            # For a directory HEAD request with a trailing slash, send 200 OK
            # and content type for HTML, but no body (as it's HEAD).
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            return  # Request handled

        # If it's not a directory (meaning it's a file request),
        # delegate to the parent SimpleHTTPRequestHandler's do_HEAD method.
        # Our overridden translate_path will ensure it operates within the specified directory.
        super().do_HEAD()

    # Removed the `send_head` override. The logic from it is now properly
    # handled by `do_GET` and `do_HEAD` which delegate to `super().do_GET()`
    # and `super().do_HEAD()` for file serving.
    # The `translate_path` override ensures the security, and `end_headers`
    # adds CORS and caching.

    def end_headers(self):
        """
        Overrides to add CORS and Cache-Control headers before sending.
        These headers are added for all responses (files, directory listings, errors).
        """
        self.send_header("Access-Control-Allow-Origin", "*")

        # Basic caching for static files (like tiles)
        #
        # For production, consider more sophisticated caching based on file hashes (ETag)
        # or more granular cache policies. For simple serving, this is good.
        #
        # Cache for 1 year (31536000 seconds) for highly static tiles
        cache_duration_seconds = 31536000  # 1 year
        self.send_header(
            "Cache-Control", f"public, max-age={cache_duration_seconds}, immutable"
        )

        # Add Expires header for older HTTP/1.0 compatibility
        expires_time = datetime.now() + timedelta(seconds=cache_duration_seconds)
        self.send_header(
            "Expires", email.utils.formatdate(expires_time.timestamp(), usegmt=True)
        )

        # Call the parent class's end_headers to send the standard headers (like Server, Date).
        super().end_headers()

    def list_directory(self, path):
        """
        Create a simple directory listing, excluding files with extensions in EXCLUDED_EXTENSIONS
        and directories in EXCLUDED_DIRECTORIES.
        """
        try:
            # Get list of files and directories in the path
            file_list = os.listdir(path)
        except OSError:
            self.send_error(404, "Directory not found")
            return None

        filtered_list = []
        for name in file_list:
            full_path = os.path.join(path, name)

            # Skip hidden files
            if name.startswith("."):
                continue

            # Skip files with excluded extensions
            excluded_file = False
            for ext in EXCLUDED_EXTENSIONS:
                if name.endswith(ext):  # Only check suffix, not name == ext.lstrip(".")
                    excluded_file = True
                    break
            if excluded_file:
                continue

            # Skip excluded directories
            if os.path.isdir(full_path) and name in EXCLUDED_DIRECTORIES:
                continue

            filtered_list.append(name)

        # Sort the filtered list for consistent display
        filtered_list.sort(key=lambda a: a.lower())

        # --- HTML generation for directory listing ---
        r = []
        r.append("<!DOCTYPE HTML>")
        r.append("<html>\n<head>")
        r.append(
            "<title>Directory listing for {}</title>".format(
                os.path.basename(path) or "/"
            )
        )
        r.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        r.append("<style>")
        r.append("body { font-family: Arial, sans-serif; margin: 5rem; }")
        r.append("h1 { border-bottom: 0.5rem solid #ccc; padding-bottom: 2.5rem; }")
        r.append("ul { list-style-type: none; padding: 0; }")
        r.append("li { margin: 0.25rem 0; }")
        r.append("a { text-decoration: none; color: #0066cc; }")
        r.append("a:hover { text-decoration: underline; }")
        r.append("</style>")
        r.append("</head>\n<body>")
        r.append(
            "<h1>Directory listing for {}</h1>".format(os.path.basename(path) or "/")
        )

        # Add information about excluded file types if any are being filtered
        if EXCLUDED_EXTENSIONS or EXCLUDED_DIRECTORIES:
            hidden_info = []
            if EXCLUDED_EXTENSIONS:
                hidden_info.append(f"file types: {', '.join(EXCLUDED_EXTENSIONS)}")
            if EXCLUDED_DIRECTORIES:
                hidden_info.append(f"directories: {', '.join(EXCLUDED_DIRECTORIES)}")
            r.append(
                f"<p><small>Note: Some {', and '.join(hidden_info)} are hidden from this listing.</small></p>"
            )

        r.append("<hr>\n<ul>")

        # Add parent directory link if not at root
        # Check if the current path is truly different from the base directory
        if os.path.normpath(path) != os.path.normpath(self.directory):
            parent_path = os.path.dirname(path)
            # Make sure parent_path is still within the base directory
            if parent_path.startswith(self.directory):
                rel_parent = os.path.relpath(parent_path, self.directory)
                parent_url = "/" + quote(rel_parent.replace(os.path.sep, "/"))
                if (
                    parent_url == "/."
                ):  # If relpath yields ".", it means it's the base directory
                    parent_url = "/"
                r.append(
                    '<li><a href="{}">Parent Directory</a></li>'.format(parent_url)
                )

        # Add files and directories
        for name in filtered_list:
            full_path_item = os.path.join(path, name)  # Use a distinct variable name

            # Get relative URL path from the base directory
            rel_path = os.path.relpath(full_path_item, self.directory)
            url_path = "/" + quote(rel_path.replace(os.path.sep, "/"))

            # Add trailing slash for directories in the displayed name and URL
            if os.path.isdir(full_path_item):
                url_path += "/"
                name += "/"  # Append slash to displayed name

            r.append('<li><a href="{}">{}</a></li>'.format(url_path, name))

        r.append("</ul>\n<hr>\n</body>\n</html>")

        # Join the HTML and create a file-like object
        encoded = "\n".join(r).encode("utf-8")
        f = io.BytesIO(encoded)

        # Send response headers for the directory listing
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()  # This will now include CORS and Cache headers

        return f


def run_server(directory, host="0.0.0.0", port=8000):
    """
    Start the HTTP server with given parameters.

    Args:
        directory: Directory to serve files from. This will be the root of the server.
        host: Host address to bind to.
        port: Port to listen on.
    """
    # Ensure the directory exists and is absolute early
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        print(
            f"Error: The specified directory '{directory}' does not exist or is not a directory.",
            file=sys.stderr,
        )
        return 1

    # Create handler with specified directory
    # We use functools.partial to pass the 'directory' argument to our custom handler's __init__
    handler = partial(RestrictedCORSAndCacheFileHandler, directory=directory)

    # Allow port reuse for faster restarts
    class ReusableAddressServer(ThreadingHTTPServer):
        allow_reuse_address = True

    # Create the server
    server = ReusableAddressServer((host, port), handler)

    # Get actual hostname for display
    display_host = host if host != "0.0.0.0" else socket.gethostname()

    # Start server
    print(f"Serving files from: '{directory}'")
    print(f"Server running at: http://{display_host}:{port}")
    print("CORS (Access-Control-Allow-Origin: *) enabled.")
    print("Caching headers set for static content.")
    if EXCLUDED_EXTENSIONS or EXCLUDED_DIRECTORIES:
        print(
            f"Excluding from listings: "
            f"Files={', '.join(EXCLUDED_EXTENSIONS) if EXCLUDED_EXTENSIONS else 'None'}, "
            f"Directories={', '.join(EXCLUDED_DIRECTORIES) if EXCLUDED_DIRECTORIES else 'None'}"
        )
    else:
        print("No file types or directories excluded from listings.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down the server...")
        server.shutdown()
        server.server_close()
        print("Server stopped.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        return 1

    return 0


def main():
    """Parse arguments and start the server."""
    parser = argparse.ArgumentParser(
        description="Serve files from a specified directory over HTTP with security, CORS, and caching.",
        epilog="Example: python3 server.py ./my_tiles -p 8080 -b 127.0.0.1",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=os.getcwd(),
        help="The base directory to serve files from (default: current directory). "
        "The server will *not* serve files from parent directories.",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8000,
        help="Port to bind the server to (default: 8000).",
    )
    parser.add_argument(
        "-b",
        "--bind",
        default="0.0.0.0",
        help="Address to bind the server to (default: 0.0.0.0 - accessible from all interfaces). "
        "Use 127.0.0.1 for local-only access.",
    )

    args = parser.parse_args()

    return run_server(args.directory, args.bind, args.port)


if __name__ == "__main__":
    sys.exit(main())
