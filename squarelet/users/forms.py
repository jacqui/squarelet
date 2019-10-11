# Django
from django import forms
from django.contrib import messages
from django.db import transaction
from django.utils.translation import ugettext_lazy as _

# Third Party
import stripe
from allauth.account import forms as allauth
from allauth.account.utils import setup_user_email
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout

# Squarelet
from squarelet.core.forms import StripeForm
from squarelet.core.layout import Field
from squarelet.core.utils import mixpanel_event
from squarelet.organizations.models import Organization, OrganizationChangeLog, Plan
from squarelet.users.models import User


class SignupForm(allauth.SignupForm, StripeForm):
    """Add a name field to the sign up form"""

    name = forms.CharField(
        max_length=255, widget=forms.TextInput(attrs={"placeholder": "Full name"})
    )

    plan = forms.ModelChoiceField(
        label=_("Plan"),
        queryset=Plan.objects.filter(public=True),
        empty_label=None,
        to_field_name="slug",
        widget=forms.HiddenInput(),
    )
    organization_name = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stripe_token"].required = False

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field("stripe_token"),
            Field("stripe_pk"),
            Field("name"),
            Field("username"),
            Field("email", type="email"),
            Field("password1", type="password", css_class="_cls-passwordInput"),
        )
        self.fields["username"].widget.attrs.pop("autofocus", None)
        self.helper.form_tag = False

    def clean(self):
        data = super().clean()
        plan = data["plan"]
        if plan.requires_payment() and not data.get("stripe_token"):
            self.add_error(
                "plan",
                _(
                    "You must supply a credit card number to sign up for a "
                    "non-free plan"
                ),
            )
        if not plan.for_individuals and not data.get("organization_name"):
            self.add_error(
                "organization_name",
                _(
                    "Organization name is required if registering an "
                    "organizational account"
                ),
            )
        return data

    @transaction.atomic()
    def save(self, request):

        user = User.objects.create_user(
            username=self.cleaned_data.get("username"),
            email=self.cleaned_data.get("email"),
            password=self.cleaned_data.get("password1"),
            name=self.cleaned_data.get("name"),
            source=request.GET.get("intent", "squarelet").lower().strip()[:11],
        )
        setup_user_email(request, user, [])
        mixpanel_event(
            request, "Sign Up", {"Source": f"Squarelet: {user.source}"}, signup=True
        )

        free_plan = Plan.objects.get(slug="free")
        plan = self.cleaned_data["plan"]
        try:
            if not plan.free() and plan.for_individuals:
                user.individual_organization.set_subscription(
                    self.cleaned_data.get("stripe_token"), plan, max_users=1, user=user
                )

            if not plan.free() and plan.for_groups:
                group_organization = Organization.objects.create(
                    name=self.cleaned_data["organization_name"],
                    plan=free_plan,
                    next_plan=free_plan,
                )
                group_organization.add_creator(user)
                group_organization.change_logs.create(
                    reason=OrganizationChangeLog.CREATED,
                    user=user,
                    to_plan=group_organization.plan,
                    to_next_plan=group_organization.next_plan,
                    to_max_users=group_organization.max_users,
                )
                group_organization.set_subscription(
                    self.cleaned_data.get("stripe_token"), plan, max_users=5, user=user
                )
                mixpanel_event(
                    request,
                    "Create Organization",
                    {
                        "Name": group_organization.name,
                        "UUID": group_organization.uuid,
                        "Plan": group_organization.plan.name,
                        "Max Users": group_organization.max_users,
                        "Sign Up": True,
                    },
                )
        except stripe.error.StripeError as exc:
            messages.error(request, "Payment error: {}".format(exc.user_message))
        return user


class LoginForm(allauth.LoginForm):
    """Customize the login form layout"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field("login", css_class="_cls-usernameInput"),
            Field("password", type="password"),
        )
        self.fields["login"].widget.attrs.pop("autofocus", None)
        self.helper.form_tag = False


class AddEmailForm(allauth.AddEmailForm):
    """Customize the add email form layout"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.layout = Layout(Field("email", type="email"))
        self.helper.form_tag = False


class ChangePasswordForm(allauth.ChangePasswordForm):
    """Customize the change password form layout"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field("oldpassword", type="password", css_class="_cls-passwordInput"),
            Field("password1", type="password", css_class="_cls-passwordInput"),
            Field("password2", type="password", css_class="_cls-passwordInput"),
        )
        self.helper.form_tag = False


class SetPasswordForm(allauth.SetPasswordForm):
    """Customize the set password form layout"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field("password1", type="password", css_class="_cls-passwordInput"),
            Field("password2", type="password", css_class="_cls-passwordInput"),
        )
        self.helper.form_tag = False


class ResetPasswordForm(allauth.ResetPasswordForm):
    """Customize the reset password form layout"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.layout = Layout(Field("email", type="email"))
        self.helper.form_tag = False


class ResetPasswordKeyForm(allauth.ResetPasswordKeyForm):
    """Customize the reset password key form layout"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Field("password1", type="password", css_class="_cls-passwordInput"),
            Field("password2", type="password", css_class="_cls-passwordInput"),
        )
        self.helper.form_tag = False
