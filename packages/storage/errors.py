"""Typed errors for the storage layer."""


class StorageError(Exception):
    """Base class for every error this layer raises."""

    status_code = 500
    code = "storage_error"


class UnsafePathError(StorageError):
    """A caller supplied an ID or filename that could escape the key namespace.

    This is a security event (§15) and must be logged as such.
    """

    status_code = 400
    code = "unsafe_path"


class UnsupportedFileTypeError(StorageError):
    """Extension is not on the allowlist."""

    status_code = 415
    code = "unsupported_file_type"


class FileTooLargeError(StorageError):
    """File exceeds the per-kind size limit."""

    status_code = 413
    code = "file_too_large"


class ObjectNotFoundError(StorageError):
    """Requested key does not exist."""

    status_code = 404
    code = "object_not_found"


class ImmutableRenderError(StorageError):
    """Attempt to overwrite an existing render. Renders are write-once."""

    status_code = 409
    code = "immutable_render"


class UnrecoverableRenderError(StorageError):
    """A partially-published render cannot be completed.

    The staging files are too old and the manifest never made it to its final
    path or to staging.  The render must be re-run from scratch with a new
    render_id.
    """

    status_code = 410
    code = "unrecoverable_render"


class ProbeError(StorageError):
    """ffprobe failed or returned unusable output."""

    status_code = 422
    code = "probe_failed"
