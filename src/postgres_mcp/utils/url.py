from urllib.parse import quote


def fix_connection_url(url: str) -> str:
    """Automatically encode special characters in the password in the connection URL"""
    try:
        if "://" in url and "@" in url:
            scheme_end = url.find("://") + 3
            at_pos = url.find("@", scheme_end)
            user_pass = url[scheme_end:at_pos]
            if ":" in user_pass:
                username, password = user_pass.split(":", 1)
                encoded_password = quote(password, safe="")
                return url[:scheme_end] + username + ":" + encoded_password + url[at_pos:]
    except Exception as e:
        print(e)
    return url
