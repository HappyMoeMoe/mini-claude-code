"""Create display-only copies of JSON arguments; never alter executable inputs."""

import re


# Match credential names, including camelCase and common separators. Do not
# mask every "key" field: lookup keys and public keys remain useful to review.
SENSITIVE_FIELD = re.compile(
    r'password|passwd|passphrase|secret|token|authorization|credential|api.?key'
    r'|(?:api|private|access|signing|encryption)[\s_.-]*key'
    r'|密码|口令|私钥|密钥|凭证',
    re.I,
)


def redact_arguments(arguments, secrets=()):
    # Replace longer values first when configured secrets overlap. Keep the
    # approval policy's minimum length to avoid masking ordinary short words.
    values = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def redact(value, key=''):
        if SENSITIVE_FIELD.search(key):
            return '<redacted>'
        if isinstance(value, dict):
            return {k: redact(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v, key) for v in value]
        if isinstance(value, str):
            for secret in values:
                value = value.replace(secret, '<redacted>')
        return value

    return redact(arguments)
