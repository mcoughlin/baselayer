from requests import HTTPError
from social_core.backends.github import GithubOAuth2


class VerifiedEmailGithubOAuth2(GithubOAuth2):
    """GitHub, reporting `email_verified` the way an OIDC provider does."""

    # Matches the /login/github route and the SOCIAL_AUTH_GITHUB_* settings.
    name = "github"
    # /user/emails is unreadable without it, leaving only a public address.
    DEFAULT_SCOPE = ["user:email"]

    def user_data(self, access_token, *args, **kwargs):
        data = self._user_data(access_token)
        try:
            emails = [
                email
                for email in (self._user_data(access_token, "/emails") or [])
                if isinstance(email, dict)
            ]
        except (HTTPError, ValueError, TypeError):
            emails = []

        # The primary address, else the first offered.
        chosen = next((e for e in emails if e.get("primary")), None) or next(
            iter(emails), None
        )
        if chosen and chosen.get("email"):
            data["email"] = chosen["email"]
            if chosen.get("verified"):
                data["email_verified"] = True
        return data
