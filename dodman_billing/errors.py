class BillingError(Exception):
    """Raised for any expected failure condition (bad plan, no active
    subscription, Paystack rejected something, etc). Framework adapters
    (router.py) catch this and translate it into their own HTTP response
    shape - the engine itself never knows about HTTP."""
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)
