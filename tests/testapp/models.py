from django.db import models


class Receipt(models.Model):
    """Stands in for a project's receipt: the fields the app reads, nothing else."""

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    invoice_no = models.CharField(max_length=50, blank=True)
    ref_no = models.CharField(max_length=250, null=True, blank=True)
    is_completed = models.BooleanField(default=False)
    meta = models.JSONField(null=True, blank=True)
