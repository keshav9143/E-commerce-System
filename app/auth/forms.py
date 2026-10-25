"""
app/auth/forms.py — WTForms form classes for authentication.

Security notes
──────────────
- Passwords are validated for minimum length server-side.  The browser's
  `minlength` attribute is a UX hint only — never rely on it for security.
- The validate_username / validate_email methods query the DB to check
  uniqueness.  They run only during registration.
- Flask-WTF automatically adds a hidden CSRF token to every form rendered
  with {{ form.hidden_tag() }} or {{ form.csrf_token }}.  Never disable CSRF
  in production (WTF_CSRF_ENABLED is only set to False in TestingConfig).
"""

from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, StringField, SubmitField
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
    ValidationError,
)


class RegistrationForm(FlaskForm):
    """New-user registration form."""

    username = StringField(
        "Username",
        validators=[
            DataRequired(message="Username is required."),
            Length(min=3, max=80, message="Username must be between 3 and 80 characters."),
        ],
        render_kw={"placeholder": "e.g. alice123", "autocomplete": "username"},
    )
    email = EmailField(
        "Email address",
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Please enter a valid email address."),
            Length(max=254),
        ],
        render_kw={"placeholder": "you@example.com", "autocomplete": "email"},
    )
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(min=8, message="Password must be at least 8 characters."),
        ],
        render_kw={"placeholder": "At least 8 characters", "autocomplete": "new-password"},
    )
    confirm_password = PasswordField(
        "Confirm password",
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo("password", message="Passwords do not match."),
        ],
        render_kw={"placeholder": "Repeat your password", "autocomplete": "new-password"},
    )
    submit = SubmitField("Create account")

    # ── Cross-field uniqueness validators ─────────────────────────────────────
    # WTForms calls validate_<fieldname>() automatically during form.validate().

    def validate_username(self, field: StringField) -> None:
        """Reject the form if the username is already taken."""
        # Import inside the method to avoid a circular import:
        # forms → models → extensions → (nothing that imports forms).
        from app.models import User

        if User.query.filter_by(username=field.data.strip()).first():
            raise ValidationError("That username is already taken. Please choose another.")

    def validate_email(self, field: EmailField) -> None:
        """Reject the form if the email address is already registered."""
        from app.models import User

        if User.query.filter_by(email=field.data.strip().lower()).first():
            raise ValidationError(
                "An account with that email address already exists. "
                "Did you mean to log in?"
            )


class LoginForm(FlaskForm):
    """Existing-user login form."""

    email = EmailField(
        "Email address",
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Please enter a valid email address."),
        ],
        render_kw={"placeholder": "you@example.com", "autocomplete": "email"},
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="Password is required.")],
        render_kw={"placeholder": "Your password", "autocomplete": "current-password"},
    )
    remember_me = BooleanField("Remember me")
    submit = SubmitField("Log in")
