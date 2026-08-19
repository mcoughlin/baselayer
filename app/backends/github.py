from requests import HTTPError
from social_core.backends.github import GithubOAuth2


class VerifiedEmailGithubOAuth2(GithubOAuth2):
    """GitHub, reporting `email_verified` the way an OIDC provider does.

    The stock backend takes the primary address from /user/emails and drops the
    `verified` flag GitHub returns beside it, so an unverified address arrives
    indistinguishable from a verified one. Anything deciding whether a sign-in
    may attach to an existing account needs them apart, or claiming an address
    at GitHub is enough to inherit someone's account.

    An unverified address is still reported, so the user gets an account with
    their email; only the claim is withheld, so it cannot be matched to an
    existing account.
    """

    # The stock name, so the /login/github route, the SOCIAL_AUTH_GITHUB_*
    # settings and the login button are unchanged by the swap.
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

        # Same address the stock backend settles on: the primary one, else the
        # first offered.
        chosen = next((e for e in emails if e.get("primary")), None) or next(
            iter(emails), None
        )
        if chosen and chosen.get("email"):
            data["email"] = chosen["email"]
            if chosen.get("verified"):
                data["email_verified"] = True
        return data
