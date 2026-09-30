class ApiError(Exception):
    """Error that is turned into the standard error envelope by the API layer."""
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or []
