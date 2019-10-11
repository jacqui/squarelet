# Django
from django.conf import settings
from django.contrib.postgres.fields import CIEmailField
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

# Standard Library
import logging
import uuid
from datetime import date

# Third Party
import stripe
from autoslug import AutoSlugField
from dateutil.relativedelta import relativedelta
from memoize import mproperty
from sorl.thumbnail import ImageField

# Squarelet
from squarelet.core.fields import AutoCreatedField, AutoLastModifiedField
from squarelet.core.mail import ORG_TO_RECEIPTS, send_mail
from squarelet.core.mixins import AvatarMixin
from squarelet.oidc.middleware import send_cache_invalidations

# Local
from .querysets import (
    ChargeQuerySet,
    InvitationQuerySet,
    OrganizationQuerySet,
    PlanQuerySet,
)

stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = "2018-09-24"

DEFAULT_AVATAR = static("images/avatars/organization.png")

logger = logging.getLogger(__name__)


class Organization(AvatarMixin, models.Model):
    """Orginization to allow pooled requests and collaboration"""

    objects = OrganizationQuerySet.as_manager()

    uuid = models.UUIDField(
        _("UUID"),
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Uniquely identify the organization across services"),
    )

    name = models.CharField(
        _("name"), max_length=255, help_text=_("The name of the organization")
    )
    slug = AutoSlugField(
        _("slug"),
        populate_from="name",
        unique=True,
        help_text=_("A unique slug for use in URLs"),
    )
    created_at = AutoCreatedField(
        _("created at"), help_text=_("When this organization was created")
    )
    updated_at = AutoLastModifiedField(
        _("updated at"), help_text=_("When this organization was last updated")
    )

    avatar = ImageField(
        _("avatar"),
        upload_to="org_avatars",
        blank=True,
        help_text=_("An image to represent the organization"),
    )

    users = models.ManyToManyField(
        verbose_name=_("users"),
        to="users.User",
        through="organizations.Membership",
        related_name="organizations",
        help_text=_("The user's in this organization"),
    )

    plan = models.ForeignKey(
        verbose_name=_("plan"),
        to="organizations.Plan",
        on_delete=models.PROTECT,
        related_name="organizations",
        help_text=_("The current plan this organization is subscribed to"),
    )
    next_plan = models.ForeignKey(
        verbose_name=_("next plan"),
        to="organizations.Plan",
        on_delete=models.PROTECT,
        related_name="pending_organizations",
        help_text=_(
            "The pending plan to be updated to on the next billing cycle - "
            "used when downgrading a plan to let the organization finish out a "
            "subscription is paid for"
        ),
    )
    individual = models.BooleanField(
        _("individual organization"),
        default=False,
        help_text=_("This organization is solely for the use of one user"),
    )
    private = models.BooleanField(
        _("private organization"),
        default=False,
        help_text=_(
            "This organization's information and membership is not made public"
        ),
    )

    # Book keeping
    max_users = models.IntegerField(
        _("maximum users"),
        default=5,
        help_text=_("The maximum number of users in this organization"),
    )
    update_on = models.DateField(
        _("date update"),
        null=True,
        blank=True,
        help_text=_("Date when monthly requests are restored"),
    )

    # stripe
    customer_id = models.CharField(
        _("customer id"),
        max_length=255,
        unique=True,
        blank=True,
        null=True,
        help_text=_("The organization's corresponding ID on stripe"),
    )
    subscription_id = models.CharField(
        _("subscription id"),
        max_length=255,
        unique=True,
        blank=True,
        null=True,
        help_text=_("The organization's corresponding subscription ID on stripe"),
    )
    payment_failed = models.BooleanField(
        _("payment failed"),
        default=False,
        help_text=_(
            "A payment for this organization has failed - they should update their "
            "payment information"
        ),
    )

    default_avatar = static("images/avatars/organization.png")

    class Meta:
        ordering = ("slug",)

    def __str__(self):
        if self.individual:
            return f"{self.name} (Individual)"
        else:
            return self.name

    def save(self, *args, **kwargs):
        # pylint: disable=arguments-differ
        with transaction.atomic():
            super().save(*args, **kwargs)
            transaction.on_commit(
                lambda: send_cache_invalidations("organization", self.uuid)
            )

    def get_absolute_url(self):
        """The url for this object"""
        if self.individual:
            # individual orgs do not have a detail page, use the user's page
            return self.user.get_absolute_url()
        else:
            return reverse("organizations:detail", kwargs={"slug": self.slug})

    @property
    def email(self):
        """Get an email for this organization"""
        if self.individual:
            return self.user.email

        receipt_email = self.receipt_emails.first()
        if receipt_email:
            return receipt_email.email

        return self.users.filter(memberships__admin=True).first().email

    # User Management
    def has_admin(self, user):
        """Is the given user an admin of this organization"""
        return self.users.filter(pk=user.pk, memberships__admin=True).exists()

    def has_member(self, user):
        """Is the user a member?"""
        return self.users.filter(pk=user.pk).exists()

    def user_count(self):
        """Count the number of users, including pending invitations"""
        return self.users.count() + self.invitations.get_pending().count()

    def add_creator(self, user):
        """Add user as the creator of the organization"""
        # add creator to the organization as an admin by default
        self.memberships.create(user=user, admin=True)
        # add the creators email as a receipt recipient by default
        # agency users may not have an email
        if user.email:
            self.receipt_emails.create(email=user.email)

    @mproperty
    def reference_name(self):
        if self.individual:
            return _("Your account")
        return self.name

    # Payment Management
    @mproperty
    def customer(self):
        """Retrieve the customer from Stripe or create one if it doesn't exist"""
        if self.customer_id:
            try:
                return stripe.Customer.retrieve(self.customer_id)
            except stripe.error.InvalidRequestError:  # pragma: no cover
                pass

        customer = stripe.Customer.create(description=self.name, email=self.email)
        self.customer_id = customer.id
        self.save()
        return customer

    @mproperty
    def subscription(self):
        if self.subscription_id:
            try:
                return stripe.Subscription.retrieve(self.subscription_id)
            except stripe.error.InvalidRequestError:  # pragma: no cover
                return None
        else:
            return None

    @mproperty
    def card(self):
        """Retrieve the customer's default credit card on file, if there is one"""
        if self.customer.default_source:
            source = self.customer.sources.retrieve(self.customer.default_source)
            if source.object == "card":
                return source
            else:
                return None
        else:
            return None

    @property
    def card_display(self):
        if self.customer_id and self.card:
            return f"{self.card.brand}: {self.card.last4}"
        else:
            return ""

    def save_card(self, token):
        self.payment_failed = False
        self.save()
        self.customer.source = token
        self.customer.save()
        send_cache_invalidations("organization", self.uuid)

    def set_subscription(self, token, plan, max_users, user):
        if self.individual:
            max_users = 1
        if token:
            self.save_card(token)

        # store so we can log
        from_plan, from_next_plan, from_max_users = (
            self.plan,
            self.next_plan,
            self.max_users,
        )

        if self.plan.free() and not plan.free():
            # create a subscription going from free to non-free
            self._create_subscription(self.customer, plan, max_users)
        elif not self.plan.free() and plan.free():
            # cancel a subscription going from non-free to free
            self._cancel_subscription(plan)
        elif not self.plan.free() and not plan.free():
            # modify a subscription going from non-free to non-free
            self._modify_subscription(self.customer, plan, max_users)
        else:
            # just change the plan without touching stripe if going free to free
            self._modify_plan(plan, max_users)

        self.change_logs.create(
            user=user,
            reason=OrganizationChangeLog.UPDATED,
            from_plan=from_plan,
            from_next_plan=from_next_plan,
            from_max_users=from_max_users,
            to_plan=self.plan,
            to_next_plan=self.next_plan,
            to_max_users=self.max_users,
        )

    @transaction.atomic
    def _create_subscription(self, customer, plan, max_users):
        """Create a subscription on stripe for the new plan"""

        def stripe_create_subscription():
            """Call this after the current transaction is committed,
            to ensure the organization is in the database before we
            receive the charge succeeded webhook
            """
            if not customer.email:  # pragma: no cover
                customer.email = self.email
                customer.save()
            subscription = customer.subscriptions.create(
                items=[{"plan": plan.stripe_id, "quantity": max_users}],
                billing="send_invoice" if plan.annual else "charge_automatically",
                days_until_due=30 if plan.annual else None,
            )
            self.subscription_id = subscription.id
            self.save()

        self.plan = plan
        self.next_plan = plan
        self.max_users = max_users
        self.update_on = date.today() + relativedelta(months=1)
        self.save()
        transaction.on_commit(stripe_create_subscription)

    def _cancel_subscription(self, plan):
        """Cancel the subscription at period end on stripe for the new plan"""
        if self.subscription is not None:
            self.subscription.cancel_at_period_end = True
            self.subscription.save()
            self.subscription_id = None
        else:  # pragma: no cover
            logger.error(
                "Attempting to cancel subscription for organization: %s %s "
                "but no subscription was found",
                self.name,
                self.pk,
            )

        self.next_plan = plan
        self.save()

    def _modify_subscription(self, customer, plan, max_users):
        """Modify the subscription on stripe for the new plan"""

        # if we are trying to modify the subscription, one should already exist
        # if for some reason it does not, then just create a new one
        if self.subscription is None:  # pragma: no cover
            logger.warning(
                "Trying to modify non-existent subscription for organization - %d - %s",
                self.pk,
                self,
            )
            self._create_subscription(customer, plan, max_users)
            return

        if not customer.email:
            customer.email = self.email
            customer.save()
        stripe.Subscription.modify(
            self.subscription_id,
            cancel_at_period_end=False,
            items=[
                {
                    # pylint: disable=unsubscriptable-object
                    "id": self.subscription["items"]["data"][0].id,
                    "plan": plan.stripe_id,
                    "quantity": max_users,
                }
            ],
            billing="send_invoice" if plan.annual else "charge_automatically",
            days_until_due=30 if plan.annual else None,
        )

        self._modify_plan(plan, max_users)

    def _modify_plan(self, plan, max_users):
        """Modify the plan without affecting stripe, for free to free transitions"""

        if plan.feature_level >= self.plan.feature_level:
            # upgrade immediately
            self.plan = plan
            self.next_plan = plan
        else:
            # downgrade at end of billing cycle
            self.next_plan = plan

        self.max_users = max_users

        self.save()

    def subscription_cancelled(self):
        """The subsctription was cancelled due to payment failure"""
        free_plan = Plan.objects.get(slug="free")
        self.change_logs.create(
            reason=OrganizationChangeLog.FAILED,
            from_plan=self.plan,
            from_next_plan=self.next_plan,
            from_max_users=self.max_users,
            to_plan=free_plan,
            to_next_plan=free_plan,
            to_max_users=self.max_users,
        )
        self.subscription_id = None
        self.plan = self.next_plan = free_plan
        self.save()

    def charge(self, amount, description, fee_amount=0, token=None, save_card=False):
        """Charge the organization and optionally save their credit card"""
        if save_card:
            self.save_card(token)
            token = None
        charge = Charge.objects.make_charge(
            self, token, amount, fee_amount, description
        )
        return charge

    def set_receipt_emails(self, emails):
        new_emails = set(emails)
        old_emails = {r.email for r in self.receipt_emails.all()}
        self.receipt_emails.filter(email__in=old_emails - new_emails).delete()
        ReceiptEmail.objects.bulk_create(
            [ReceiptEmail(organization=self, email=e) for e in new_emails - old_emails]
        )


class Membership(models.Model):
    """Through table for organization membership"""

    user = models.ForeignKey(
        verbose_name=_("user"),
        to="users.User",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    organization = models.ForeignKey(
        verbose_name=_("organization"),
        to="organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    admin = models.BooleanField(
        _("admin"),
        default=False,
        help_text=_("This user has administrative rights for this organization"),
    )

    class Meta:
        unique_together = ("user", "organization")

    def __str__(self):
        return f"Membership: {self.user} in {self.organization}"

    def save(self, *args, **kwargs):
        # pylint: disable=arguments-differ
        with transaction.atomic():
            super().save(*args, **kwargs)
            transaction.on_commit(
                lambda: send_cache_invalidations("user", self.user.uuid)
            )

    def delete(self, *args, **kwargs):
        # pylint: disable=arguments-differ
        with transaction.atomic():
            super().delete(*args, **kwargs)
            transaction.on_commit(
                lambda: send_cache_invalidations("user", self.user.uuid)
            )


class Plan(models.Model):
    """Plans that organizations can subscribe to"""

    objects = PlanQuerySet.as_manager()

    name = models.CharField(_("name"), max_length=255, help_text=_("The plan's name"))
    slug = AutoSlugField(
        _("slug"),
        populate_from="name",
        unique=True,
        help_text=_("A uinique slug to identify the plan"),
    )

    minimum_users = models.PositiveSmallIntegerField(
        _("minimum users"),
        default=1,
        help_text=_("The minimum number of users allowed on this plan"),
    )
    base_price = models.PositiveSmallIntegerField(
        _("base price"),
        default=0,
        help_text=_(
            "The price per month for this plan with the minimum number of users"
        ),
    )
    price_per_user = models.PositiveSmallIntegerField(
        _("price per user"),
        default=0,
        help_text=_("The additional cost per month per user over the minimum"),
    )

    feature_level = models.PositiveSmallIntegerField(
        _("feature level"),
        default=0,
        help_text=_("Specifies the level of premium features this plan grants"),
    )

    public = models.BooleanField(
        _("public"),
        default=False,
        help_text=_("Is this plan available for anybody to sign up for?"),
    )
    annual = models.BooleanField(
        _("annual"),
        default=False,
        help_text=_("Invoice this plan annually instead of charging monthly"),
    )
    for_individuals = models.BooleanField(
        _("for individuals"),
        default=True,
        help_text=_("Is this plan usable for individual organizations?"),
    )
    for_groups = models.BooleanField(
        _("for groups"),
        default=True,
        help_text=_("Is this plan usable for non-individual organizations?"),
    )

    private_organizations = models.ManyToManyField(
        verbose_name=_("private organizations"),
        to="organizations.Organization",
        related_name="private_plans",
        help_text=_(
            "For private plans, organizations which should have access to this plan"
        ),
    )

    def __str__(self):
        return self.name

    def free(self):
        return self.base_price == 0 and self.price_per_user == 0

    def requires_payment(self):
        """Does this plan require immediate payment?
        Free plans never require payment
        Annual payments are invoiced and do not require payment at time of purchase
        """
        return not self.free() and not self.annual

    def cost(self, users):
        return (
            self.base_price + max(users - self.minimum_users, 0) * self.price_per_user
        )

    @property
    def stripe_id(self):
        """Namespace the stripe ID to not conflict with previous plans we have made"""
        return f"squarelet_plan_{self.slug}"

    def make_stripe_plan(self):
        """Create the plan on stripe"""
        if not self.free():
            try:
                # set up the pricing for groups and individuals
                # convert dollar amounts to cents for stripe
                if self.for_groups:
                    kwargs = {
                        "billing_scheme": "tiered",
                        "tiers": [
                            {
                                "flat_amount": 100 * self.base_price,
                                "up_to": self.minimum_users,
                            },
                            {"unit_amount": 100 * self.price_per_user, "up_to": "inf"},
                        ],
                        "tiers_mode": "graduated",
                    }
                else:
                    kwargs = {
                        "billing_scheme": "per_unit",
                        "amount": 100 * self.base_price,
                    }
                stripe.Plan.create(
                    id=self.stripe_id,
                    currency="usd",
                    interval="year" if self.annual else "month",
                    product={"name": self.name, "unit_label": "Seats"},
                    **kwargs,
                )
            except stripe.error.InvalidRequestError:  # pragma: no cover
                # if the plan already exists, just skip
                pass

    def delete_stripe_plan(self):
        """Remove a stripe plan"""
        try:
            plan = stripe.Plan.retrieve(id=self.stripe_id)
            # We also want to remove the associated product
            product = stripe.Product.retrieve(id=plan.product)
            plan.delete()
            product.delete()
        except stripe.error.InvalidRequestError:
            # if the plan or product do not exist, just skip
            pass


class Invitation(models.Model):
    """An invitation for a user to join an organization"""

    objects = InvitationQuerySet.as_manager()

    organization = models.ForeignKey(
        verbose_name=_("organization"),
        to="organizations.Organization",
        related_name="invitations",
        on_delete=models.CASCADE,
        help_text=_("The organization this invitation is for"),
    )
    uuid = models.UUIDField(
        _("UUID"),
        default=uuid.uuid4,
        editable=False,
        help_text=_("This UUID serves as a secret token for this invitation in URLs"),
    )
    email = models.EmailField(
        _("email"), help_text=_("The email address to send this invitation to")
    )
    user = models.ForeignKey(
        verbose_name=_("user"),
        to="users.User",
        related_name="invitations",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        help_text=_(
            "The user this invitation is for.  Used if a user requested an "
            "invitation directly as opposed to an admin inviting them via email."
        ),
    )
    request = models.BooleanField(
        _("request"),
        help_text="Is this a request for an invitation from the user or an invitation "
        "to the user from an admin?",
        default=False,
    )
    created_at = AutoCreatedField(
        _("created at"), help_text=_("When this invitation was created")
    )
    accepted_at = models.DateTimeField(
        _("accepted at"),
        blank=True,
        null=True,
        help_text=_(
            "When this invitation was accepted.  NULL signifies it has not been "
            "accepted yet"
        ),
    )
    rejected_at = models.DateTimeField(
        _("rejected at"),
        blank=True,
        null=True,
        help_text=_(
            "When this invitation was rejected.  NULL signifies it has not been "
            "rejected yet"
        ),
    )

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"Invitation: {self.uuid}"

    def send(self):
        send_mail(
            subject=_(f"Invitation to join {self.organization.name}"),
            template="organizations/email/invitation.html",
            to=[self.email],
            extra_context={"invitation": self},
        )

    @transaction.atomic
    def accept(self, user=None):
        """Accept the invitation"""
        if self.user is None and user is None:
            raise ValueError(
                "Must give a user when accepting if invitation has no user"
            )
        if self.accepted_at or self.rejected_at:
            raise ValueError("This invitation has already been closed")
        if self.user is None:
            self.user = user
        self.accepted_at = timezone.now()
        self.save()
        if not self.organization.has_member(self.user):
            Membership.objects.create(organization=self.organization, user=self.user)

    def reject(self):
        """Reject or revoke the invitation"""
        if self.accepted_at or self.rejected_at:
            raise ValueError("This invitation has already been closed")
        self.rejected_at = timezone.now()
        self.save()

    def get_name(self):
        """Returns the name or email if no name is set"""
        if self.user is not None and self.user.name:
            return self.user.name
        else:
            return self.email


class ReceiptEmail(models.Model):
    """An email address to send receipts to"""

    organization = models.ForeignKey(
        verbose_name=_("organization"),
        to="organizations.Organization",
        related_name="receipt_emails",
        on_delete=models.CASCADE,
        help_text=_("The organization this receipt email corresponds to"),
    )
    email = CIEmailField(
        _("email"), help_text=_("The email address to send the receipt to")
    )
    failed = models.BooleanField(
        _("failed"),
        default=False,
        help_text=_("Has sending to this email address failed?"),
    )

    class Meta:
        unique_together = ("organization", "email")

    def __str__(self):
        return f"Receipt Email: <{self.email}>"


class Charge(models.Model):
    """A payment charged to an organization through Stripe"""

    objects = ChargeQuerySet.as_manager()

    amount = models.PositiveIntegerField(_("amount"), help_text=_("Amount in cents"))
    fee_amount = models.PositiveSmallIntegerField(
        _("fee amount"), default=0, help_text=_("Fee percantage")
    )
    organization = models.ForeignKey(
        verbose_name=_("organization"),
        to="organizations.Organization",
        related_name="charges",
        on_delete=models.PROTECT,
        help_text=_("The organization charged"),
    )
    created_at = models.DateTimeField(
        _("created at"), help_text=_("When the charge was created")
    )
    charge_id = models.CharField(
        _("charge_id"),
        max_length=255,
        unique=True,
        help_text=_("The strip ID for the charge"),
    )

    description = models.CharField(
        _("description"),
        max_length=255,
        help_text=_("A description of what the charge was for"),
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"${self.amount / 100:.2f} charge to {self.organization.name}"

    def get_absolute_url(self):
        return reverse("organizations:charge", kwargs={"pk": self.pk})

    @mproperty
    def charge(self):
        return stripe.Charge.retrieve(self.charge_id)

    @property
    def amount_dollars(self):
        return self.amount / 100.0

    def send_receipt(self):
        """Send receipt"""
        send_mail(
            subject=_("Receipt"),
            template="organizations/email/receipt.html",
            organization=self.organization,
            organization_to=ORG_TO_RECEIPTS,
            extra_context={
                "charge": self,
                "individual_subscription": self.description == "Professional",
                "group_subscription": self.description.startswith("Organization"),
            },
        )

    def items(self):
        if self.fee_amount:
            fee_multiplier = 1 + (self.fee_amount / 100.0)
            base_price = int(self.amount / fee_multiplier)
            fee_price = self.amount - base_price
            return [
                {"name": self.description, "price": base_price / 100},
                {"name": "Processing Fee", "price": fee_price / 100},
            ]
        else:
            return [{"name": self.description, "price": self.amount_dollars}]


class OrganizationChangeLog(models.Model):
    """Track important changes to organizations"""

    CREATED = 0
    UPDATED = 1
    FAILED = 2

    created_at = AutoCreatedField(
        _("created at"), help_text=_("When the organization was changed")
    )

    organization = models.ForeignKey(
        verbose_name=_("organization"),
        to="organizations.Organization",
        on_delete=models.CASCADE,
        related_name="change_logs",
        help_text=_("The organization which changed"),
    )
    user = models.ForeignKey(
        verbose_name=_("user"),
        to="users.User",
        related_name="change_logs",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        help_text=_("The user who changed the organization"),
    )
    reason = models.PositiveSmallIntegerField(
        _("reason"),
        choices=(
            (CREATED, _("Created")),
            (UPDATED, _("Updated")),
            (FAILED, _("Payment failed")),
        ),
        help_text=_("Which category of change occurred"),
    )

    from_plan = models.ForeignKey(
        verbose_name=_("from plan"),
        to="organizations.Plan",
        on_delete=models.PROTECT,
        related_name="+",
        blank=True,
        null=True,
        help_text=_("The organization's plan before the change occurred"),
    )
    from_next_plan = models.ForeignKey(
        verbose_name=_("from next plan"),
        to="organizations.Plan",
        on_delete=models.PROTECT,
        related_name="+",
        blank=True,
        null=True,
        help_text=_("The organization's next_plan before the change occurred"),
    )
    from_max_users = models.IntegerField(
        _("maximum users"),
        blank=True,
        null=True,
        help_text=_("The organization's max_users before the change occurred"),
    )

    to_plan = models.ForeignKey(
        verbose_name=_("to plan"),
        to="organizations.Plan",
        on_delete=models.PROTECT,
        related_name="+",
        help_text=_("The organization's plan after the change occurred"),
    )
    to_next_plan = models.ForeignKey(
        verbose_name=_("to next plan"),
        to="organizations.Plan",
        on_delete=models.PROTECT,
        related_name="+",
        help_text=_("The organization's plan after the change occurred"),
    )
    to_max_users = models.IntegerField(
        _("maximum users"),
        help_text=_("The organization's max_users after the change occurred"),
    )
