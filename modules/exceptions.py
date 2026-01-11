class UserError(Exception):
    """Base exception for errors that should be displayed to the user."""


class InsufficientFundsError(UserError):
    """Raised when a user doesn't have enough cash for a transaction."""
