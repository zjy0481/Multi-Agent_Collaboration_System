# Commit Guidelines for Agents

Apply these rules whenever you create a Git commit.

## Required Format

```text
<type>(<scope>)<!>: <Chinese summary> / <English summary>

Chinese: <what changed, why, and its impact>
English: <what changed, why, and its impact>

<footer>
```

- `type` is required.
- `scope` is optional. Omit the parentheses when no scope is useful.
- Add `!` immediately before `:` for a breaking change.
- Write both summaries in the imperative mood and separate them with ` / `.
- Keep the header within 100 characters when practical, and never exceed 120 characters.
- Do not end either summary with punctuation.
- Use a lowercase English summary after the separator.
- Keep the Chinese and English text semantically equivalent.
- Each commit must contain one logical change.

## Allowed Types

- `feat`: new functionality
- `fix`: bug fix
- `docs`: documentation only
- `style`: formatting without behavior changes
- `refactor`: code restructuring without a feature or fix
- `perf`: performance improvement
- `test`: test additions or changes
- `build`: build system or dependency changes
- `ci`: CI/CD changes
- `chore`: maintenance work
- `revert`: revert an earlier commit

Use a short, stable, lowercase English noun for `scope`, such as `api`, `auth`, `ui`, `database`, `config`, or `deps`.

## Body and Footer

The body is optional for a simple commit. Include it for a complex change, bug fix, or breaking change. Explain the reason and impact rather than restating the implementation.

Use these footer forms when applicable:

```text
Refs: #123
Closes: #456
BREAKING CHANGE: <Chinese description> / <English description>
Co-authored-by: Name <email@example.com>
```

A breaking change must include both `!` in the header and a bilingual `BREAKING CHANGE` footer.

## Examples

```text
feat(auth): 增加邮箱验证码登录 / add email verification code login
```

```text
fix(api): 修复重复请求导致的数据覆盖 / prevent data overwrite caused by duplicate requests

Chinese: 为写入接口增加幂等校验，避免客户端重试时覆盖已有数据。
English: Add idempotency validation to write endpoints to prevent retries from overwriting existing data.

Closes: #248
```

```text
feat(api)!: 统一分页参数命名 / standardize pagination parameter names

Chinese: 将 pageSize 重命名为 limit，并将 page 重命名为 offset。
English: Rename pageSize to limit and page to offset.

BREAKING CHANGE: 客户端必须更新分页参数 / clients must update pagination parameters
```

## Completion Check

Before committing, verify that every item below is true:

- The header matches the required format.
- The type and scope accurately identify the change.
- The Chinese and English descriptions have the same meaning.
- The commit represents one logical change.
- Complex, fix, and breaking commits include a bilingual body.
- Breaking changes include both required markers.
- Relevant issues are referenced or closed.
- Tests and project checks relevant to the change pass.
- The commit contains no secrets or unrelated files.

