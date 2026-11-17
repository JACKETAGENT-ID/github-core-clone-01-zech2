# zech-forge2 — hard-requirements checklist (from RULES.txt + kit check sources)

Competition: github-core-clone-01. Entry: `python3 /bot/main.py` (Python 3.13, stdlib only,
one self-contained file). Viewer login is deterministic from the fixed scenario seed:
`adaiversen` (wire.ts `buildScenario`/`generateUsers`, seed 0x6A09E667F3BCC909; token =
MAKEFUN_SHOP_TOKEN env).

## DIAGNOSIS — why v1 (zech-forge) scored 0 on the server
- v1 binds `127.0.0.1` (`ForgeServer(..., host="127.0.0.1")`).
- The env records `req.socket.remoteAddress` + the registered port as the entry base URL
  (wire.ts `entryBase`) and dials THAT address. In the grading pod (docker driver, entry and
  env in separate containers on their own network) the register request's peer address is the
  entry container's network IP (e.g. 172.x.x.x), NOT 127.0.0.1.
- A loopback-only listener refuses connections on the container IP, so the env's boot check
  (GET /healthz twice within 10 s) fails → boot failure → working=0, feature=0, agent=0,
  perf=0, even though every feature may have been correct.
- The bug is invisible in local runs: the process driver runs everything on 127.0.0.1, so
  grade-local passes locally while the server scores 0.
- FIX (v2): bind `0.0.0.0` (all interfaces), never 127.0.0.1. Verify locally by starting the
  forge and dialing it via a non-loopback local IP (e.g. the VM's eth0 address).

## Lifecycle (main.py)
- [ ] Bind 0.0.0.0 port 0 (never 8080), POST {"port": N} to $MAKEFUN_ENV_URL/register, poll
      GET $MAKEFUN_ENV_URL/done until {"done": true}, exit 0. Retry register/poll.
- [ ] GET /healthz -> 200 {"ok": true}, no auth.
- [ ] All state in memory (objects + refs + repos/issues/PRs); pack cache per repo, cleared on
      mutation. No wall clock / randomness in any response: logical clock (counter from
      1735689600), incrementing X-GitHub-Request-Id.
- [ ] Single socket write per response; TCP_NODELAY; ThreadingHTTPServer, HTTP/1.1 keep-alive,
      Content-Length always set; listen backlog >= 128.

## Protocol (all surfaces)
- [ ] Every REST reply: application/json, non-empty X-GitHub-Request-Id, JSON {"message": str}
      on errors. Unknown route -> JSON 404. Malformed JSON body -> 400.
- [ ] Auth: Bearer token must equal MAKEFUN_SHOP_TOKEN; missing/wrong -> 401 (REST, GraphQL,
      MCP, git). Same repo name under another owner -> 404, never 403.
- [ ] GET /api/v3/ -> 200 + X-OAuth-Scopes header ("repo, read:org"). GET /api/v3/meta ->
      installed_version non-empty. GET /api/v3/user -> {login}.

## REST v3
- [ ] POST /user/repos -> 201 {name, private, default_branch:"main", ...}; duplicate name -> 422
      with "already exists" in message. GET /user/repos paginates (per_page, Link rel="next",
      no repeats, >=3 pages for 30 repos at per_page=10).
- [ ] GET /repos/{o}/{r} -> 200; 404 when absent. PATCH -> 200 description updated.
      DELETE -> 204, later GET 404. Repo ids numeric, stable across reads.
- [ ] POST /repos/{o}/{r}/git/refs -> 201 {ref, object:{sha}}. GET /repos/{o}/{r}/git/refs/heads/{branch}
      and GET /branches/{branch} -> 200. GET /branches [{name, commit:{sha}}].
      GET /commits[?sha=ref] newest-first, branch-only commits excluded from default-branch list.
- [ ] PUT contents -> 201 create / 200 update / 409 stale sha; GET file {type:"file",
      encoding:"base64", content, sha, size} and directory listing array [{type,name,path,sha}];
      DELETE -> 200 {commit:{sha}}, later GET 404. PUT with branch= creates the branch from
      default tip when missing; unborn repo accepts first PUT. Blob sha == content sha.
      GET contents honours ?ref=<branch>.
- [ ] POST labels -> 201 {name, color}. POST issues -> 201 open issue, 422 unknown label.
      Issues + PRs share ONE number counter per repo, starting at 1. GET issues filters
      state open/closed/all, sort created asc/desc (default desc). PATCH closes. Comments
      POST 201 {id, body}, GET lists them.
- [ ] POST pulls -> 201 {number, state:"open", head:{ref,sha}, base:{ref,sha}}; head sha = head
      branch tip, base sha = base tip; duplicate open PR from same head -> 422. GET pulls/{n}
      200 / 404 unknown. GET pulls/{n}/files [{filename, status, additions, deletions,
      changes}]. PUT merge -> 200 {sha, merged:true, message}; squash commit on default branch
      with merged content; PR closed+merged with merged_at + merge_commit_sha; 404 unknown PR;
      405 conflicted PR (both sides changed same file differently vs merge-base). GET pulls
      defaults to open only; ?state=all lists merged too.

## Git smart HTTP
- [ ] GET /{o}/{r}.git/info/refs?service=... -> 404 for missing repo (server stays up).
      Honour Git-Protocol: version=2 header for upload-pack (v2 caps) vs v0.
- [ ] v2 (Git-Protocol: version=2): first pkt-line "version 2"; caps include
      ls-refs=unborn peel symrefs ref-prefix, fetch=shallow filter, object-format=sha1.
      v0: "# service=git-upload-pack" preamble + flush; first ref line carries caps after NUL
      (side-band-64k, shallow, multi_ack, ofs-delta, symref=HEAD:...); empty repo advertises
      zero-oid capabilities^{} line. Receive-pack v0: "# service=git-receive-pack" + caps
      report-status side-band-64k delete-refs ofs-delta.
- [ ] ls-refs: command=ls-refs + delim + peel/symrefs/unborn/ref-prefix args; HEAD line with
      symref-target, annotated tags peeled, unborn HEAD for empty repo, server-side prefix filter.
- [ ] fetch v2: sections acknowledgments (NAK), shallow-info, packfile (sideband band 1),
      each section ended by delim, whole response by flush. depth N = N commits per path;
      shallow boundary commits listed in shallow-info; deepen moves boundary (shallow new tip +
      unshallow old tip). Incremental fetch sends only objects not reachable from haves.
      Nothing-new fetch sends an empty (0-object) pack (still >=1 sideband packet).
- [ ] fetch v0: want/have/done two-section request (flush between wants and haves); NAK +
      shallow/unshallow lines + sideband pack. Requires side-band-64k advertised.
- [ ] receive-pack v0 only: commands "<old> <new> <ref>[\0caps]" + flush + raw PACK.
      Parse PACK incl. OFS_DELTA/REF_DELTA. Response: sideband band-1 pkt-lines
      "unpack ok" + "ok <ref>" / "ng <ref> non-fast-forward". Fast-forward enforced per ref;
      mixed push applies ok ref, refuses other; delete works. Objects stored even for refused refs.
- [ ] Packs: PACK v2, valid trailer SHA-1, plain objects (no deltas required on send).
      Object ids recompute (SHA-1 over "<type> <size>\0"+bytes). Commit/tree/tag bytes
      round-trip exact.
- [ ] Malformed POST to upload-pack must not crash the server (answer 400/empty, stay up).

## GraphQL v4 (POST /api/graphql)
- [ ] Auth 401 without token. Batch: JSON array of operations -> array of results in order.
- [ ] viewer{login}; repository(owner,name) with id (opaque base64, != plain db id), databaseId,
      name, nameWithOwner, owner{login}, defaultBranchRef{name}, viewerPermission (ADMIN),
      hasIssuesEnabled true; unknown repo -> data.repository null, no errors.
- [ ] __type Issue fields include id, number, title, state, body; PullRequest: id, number,
      headRefName, baseRefName, mergeable, state, merged, files; Repository: id, databaseId,
      name, nameWithOwner, owner, defaultBranchRef, viewerPermission, hasIssuesEnabled, issue,
      issues, pullRequests, pullRequest; SearchType enumValues include ISSUE.
- [ ] repository.issues(first, states:[OPEN], orderBy:{CREATED_AT DESC}) -> numbers DESC,
      closed omitted; issues(first, after) cursor pagination with pageInfo{hasNextPage,endCursor}.
- [ ] Mutations: createIssue, closeIssue, createPullRequest (head branch pushed over git),
      mergePullRequest (SQUASH), addComment — all visible over REST with same values.
- [ ] node(id:) round-trips repository (inline fragment ... on Repository). Unknown field ->
      HTTP 200 + errors[] naming it. Global ids stable across reads.

## MCP (POST /mcp)
- [ ] server/discover -> supportedVersions ["2026-07-28"], capabilities.tools present,
      serverInfo name+version non-empty (also in _meta io.modelcontextprotocol/serverInfo).
      Legacy initialize fallback supported (initialize + notifications/initialized).
- [ ] tools/list >= 6 tools incl. create_repo, get_repo, create_issue, list_issues,
      create_pull_request, merge_pull_request; every tool non-empty description + inputSchema
      type object; two calls byte-identical.
- [ ] tools/call echoes JSON-RPC id; unknown tool -> -32602/-32601 (never HTTP 500);
      missing required args -> isError:true or -32602; non-JSON body -> HTTP 400 + -32700;
      no Authorization -> HTTP 401. Tools act on the same store as REST (ids stable).
      Tool args use canonical property names (owner, repo, name, title, body, head, base,
      pull_number, description, private, state); also accept full_name "owner/repo".
- [ ] Notifications (no id) -> HTTP 202 empty. Two concurrent calls keep their own ids.

## Performance (T_ref: push 1640.82ms, clone 266.94ms, read 2759.72ms, write 348.97ms)
- [ ] In-memory store; pack cache per (repo version, wants, depth, haves) for clones;
      keep-alive HTTP/1.1; ThreadingHTTPServer; no disk I/O on hot paths.
- [ ] push: 200 sequential receive-pack exchanges. clone: 50 concurrent depth-1 clones.
      read: 2000 sequential GET issue on one connection. write: 200 sequential issue creates.

## Determinism / replay
- [ ] Same requests in same order -> same visible behaviour; no Date.now/random in responses.
- [ ] Objects immutable once stored; refs updated atomically under one lock.

---

## Hasil uji practice (env server kit dijalankan langsung, 17 Nov 2026)

| run | checks | perf |
|-----|--------|------|
| 1 | 102/120, working 1 | 170 |
| 2 | 119/120, working 1 | 169 |
| 3 | **120/120, working 1** | 133 |
| 4 | **120/120, working 1** | 135 |

Conformance deterministik 120/120 (2x). Poin perf berfluktuasi 133-170 antar-run pada VM bersama (noise CPU); jalur read lokal ~0,35 ms/request. Proyeksi skor server = 600 + 200 + 133-170 = 933-970.
