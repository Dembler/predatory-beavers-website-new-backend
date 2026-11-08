from predatory_beavers.api.errors import ConflictError


class PublicationDependencyError(ConflictError):
    """A draft cannot be published before its editorial parents."""

    code = "publication_dependency"
