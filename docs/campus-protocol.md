# Campus protocol evidence

2026-10-19: the original RSA `ic-web/login/user` implementation did not
authenticate the user's working unified-authentication credentials. Public
school sources confirm that the current seat website uses `loginMode: 2`
(CAS). The local client now follows that official flow, without WebVPN:

1. GET `ic-web/auth/address` with the current site's `finalAddress`,
   `errPageUrl`, `manager=false`, and `consoleType=16` parameters.
2. Follow the returned `libseat.njfu.edu.cn/authcenter/toLoginPage` address
   to `https://uia.njfu.edu.cn/authserver/login`.
3. Parse the current password form's hidden fields and AES encryption salt.
   Check `needCaptcha.html` before submitting any password. Stop explicitly
   when captcha or secondary verification is required; do not retry a POST.
4. Submit the AES-CBC/PKCS7 password and dynamic form fields only to the
   school's HTTPS authentication endpoint; follow 302/303 responses as GETs.
   Its callback may return HTTP 200 with a literal `window.location.href`
   navigation to `ic-web//auth/token`. Parse that literal without executing
   JavaScript; require the official host/path and the original UUID before
   completing the token exchange.
5. GET `ic-web/auth/userInfo`; require matching `logonName`, an account number
   and a token. Attach that token only to requests to the seat-system host,
   as the official frontend does. Preserve the `ic-cookie` issued by the
   token exchange; the old hand-built RSA cookie breaks authenticated queries.
   Sessions remain isolated per account.

The public CAS redirect currently advertises an HTTP service callback under
the seat host's `/authcenter/` path. The client upgrades that callback to
HTTPS before sending a ticket. This callback completed successfully in the
2026-10-19 real login verification; no TLS checks are disabled.

Public sources inspected: `https://libseat.njfu.edu.cn/config/config.js`,
`https://libseat.njfu.edu.cn/js/index_1789371053278.js`,
`https://libseat.njfu.edu.cn/js/chunk-common_1789371053278.js`, and the login
page's `login-wisedu_v1.0.js` and `encrypt.js` under the school auth host.
No real credentials, tickets, page sessions, or token values are retained
in protocol evidence. Offline fixtures contain only synthetic values.

GET `ic-web/reserve/resvInfo` is the authenticated self-query endpoint.
Records remain associated with that verified session; an explicit returned
identity is also checked on confirmation. At 2026-10-19 21:33 Beijing time,
the local app verified the user's real school identity and queried records
successfully. The UI query at 21:34 also completed. The date range was today
through three days ahead and returned an empty list, not a login error.
No real reservation or cancellation was submitted.

The original UI labels 1027/1029/1093 as active states, but these labels and
retryable reservation rejection codes need current school verification.
Production automatic confirmation/retry must remain conservative until then.
No VPN, cancellation, late-protection, or live reservation request is used in
the offline tests. Captcha or malformed/changed responses are explicit errors.
