# Django
from celery.schedules import crontab
from celery.task import periodic_task, task
from django.db.models import F
from django.utils.timezone import get_current_timezone
from django.utils.translation import ugettext_lazy as _

# Standard Library
import logging
from datetime import date, datetime

# Third Party
import stripe

# Squarelet
from squarelet.core.mail import ORG_TO_ADMINS, send_mail
from squarelet.core.models import Interval
from squarelet.oidc.middleware import send_cache_invalidations

# Local
from .models import Charge, Organization

logger = logging.getLogger(__name__)


@periodic_task(
    run_every=crontab(hour=0, minute=5),
    name="squarelet.organizations.tasks.restore_organizations",
)
def restore_organization():
    """Monthly update of organizations subscriptions"""
    organizations = Organization.objects.filter(update_on__lte=date.today())
    # convert to a list so it can be serialized by celery
    uuids = list(organizations.values_list("uuid", flat=True))
    # if the next plan is free, update_on is set to null
    organizations.filter(next_plan__base_price=0, next_plan__price_per_user=0).update(
        update_on=None, plan=F("next_plan")
    )
    # if the next plan is not free, update_on is set to 1 month from now
    organizations.exclude(next_plan__base_price=0, next_plan__price_per_user=0).update(
        update_on=date.today() + Interval("1 month"), plan=F("next_plan")
    )
    send_cache_invalidations("organization", uuids)


@task(
    name="squarelet.organizations.tasks.handle_charge_succeeded",
    autoretry_for=(Organization.DoesNotExist,),
)
def handle_charge_succeeded(charge_data):
    """Handle receiving a charge.succeeded event from the Stripe webhook"""

    # We autorety if the organization does not exist, as that should mean the webhook
    # is being processed before the database synced the customer id to the organization
    # We can't use transaction.on_commit since we need to return the Charge object
    # when we create it

    if charge_data["customer"] is None:
        # Customer should only be blank for anonymous donations or crowdfunds
        # from MuckRock - no need to log those here
        return

    if charge_data["invoice"]:
        # fetch the invoice from stripe if one associated with the charge
        invoice = stripe.Invoice.retrieve(charge_data["invoice"])
        invoice_line = invoice["lines"]["data"][0]

    def get_description():
        """Get the description from the charge data"""
        if charge_data["invoice"]:
            # depends on new or old version of API - MuckRock still uses old,
            # Squarelet uses new
            if "name" in invoice_line["plan"]:
                return invoice_line["plan"]["name"]
            else:
                return stripe.Product.retrieve(invoice_line["plan"]["product"])["name"]
        else:
            return charge_data["description"]

    # do not send receipts for MuckRock donations and crowdfunds
    if charge_data["invoice"] and invoice_line["plan"]["id"].startswith(
        ("donate", "crowdfund")
    ):
        return
    if not charge_data["invoice"] and charge_data["metadata"].get("action") in [
        "donation",
        "crowdfund-payment",
    ]:
        return

    charge, _ = Charge.objects.get_or_create(
        charge_id=charge_data["id"],
        defaults={
            "amount": charge_data["amount"],
            "fee_amount": int(charge_data["metadata"].get("fee amount", 0)),
            "organization": lambda: Organization.objects.get(
                customer_id=charge_data["customer"]
            ),
            "created_at": datetime.fromtimestamp(
                charge_data["created"], tz=get_current_timezone()
            ),
            "description": get_description,
        },
    )

    charge.send_receipt()


@task(name="squarelet.organizations.tasks.handle_invoice_failed")
def handle_invoice_failed(invoice_data):
    """Handle receiving a invoice.payment_failed event from the Stripe webhook"""
    try:
        organization = Organization.objects.get(customer_id=invoice_data["customer"])
    except Organization.DoesNotExist:
        logger.error(
            "Invoice failed (%s) for customer (%s) with no matching organization",
            invoice_data["id"],
            invoice_data["customer"],
        )
        return

    organization.payment_failed = True
    organization.save()

    logger.info("Payment failed: %s", invoice_data)

    attempt = invoice_data["attempt_count"]
    if attempt == 4:
        subject = _("Your subscription has been cancelled")
        organization.subscription_cancelled()
    else:
        subject = _("Your payment has failed")

    send_mail(
        subject=subject,
        template="organizations/email/payment_failed.html",
        organization=organization,
        organization_to=ORG_TO_ADMINS,
        extra_context={"attempt": "final" if attempt == 4 else attempt},
    )
