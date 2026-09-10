"""Delivery crossed an external boundary and must not be blindly retried."""


class DeliveryUncertainError(RuntimeError):
    def __init__(self, message: str, receipt: dict | None = None):
        super().__init__(message)
        self.receipt = receipt or {}
