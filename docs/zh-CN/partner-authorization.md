# 合作方授权

[English](../partner-authorization.md) | [简体中文](partner-authorization.md)

`PartnerAuthorization` 是核心层（Core）提供的契约，用于在两个已安装的服务之间传递短期有效的登录 URL，且不展示、不序列化、也不持久化该 URL。它仅适用于明确加入白名单的提供方关系（即由一个服务授权另一个独立服务），不是通用的浏览器链接模型，也不允许服务之间互相调用。

```text
producer service               core                     consumer service
     |                           |                              |
     | call its /authorize API   |                              |
     | create PartnerAuthorization                             |
     | yield PartnerHandoff ---->| validate and build receiver |
     |                           |----------------------------->|
     |                           |       claim URL once         |
     |                           |       complete provider SSO  |
     |                           |       save consumer session  |
     |<--------------------------| PartnerAuthorizationResult   |
```

该 URL 仅归属于本次传递本身，不属于配置或运行时状态。生产方从自身 API 获取 URL，核心层在内存中完成传递，消费方立即将其兑换为归属于自身的会话状态。

## 契约对象

该契约定义在 `unidl.core.partner` 中。

### `PartnerAuthorization`

```python
PartnerAuthorization(
    url,
    source_service_id="producer",
    service_id="consumer",
    provider="Consumer display name",
    max_age=300.0,
)
```

| 字段 | 含义 |
|-------|---------|
| `url` | HTTPS 交接 URL。读取该属性时会认领并擦除该 URL。 |
| `source_service_id` | 获取该授权的已注册服务 ID。 |
| `service_id` | 允许消费该授权的已注册服务 ID。 |
| `provider` | 用于状态提示的非敏感显示名称。 |
| `max_age` | 内存中的有效时长（秒），默认为 5 分钟。 |
| `host` | 只读的非敏感主机名元数据。 |
| `claimed` | 该 URL 是否已被消费、过期或丢弃。 |

构造时会拒绝非 HTTPS URL、无效的服务 ID 以及非正数的有效时长。元数据均为只读。`repr()` 会对 URL 脱敏，对象禁止 pickle 序列化，第二次读取 `.url` 会抛出 `PartnerAuthorizationError`。过期检查基于单调时钟，并在抛出异常前擦除 URL。

### `PartnerHandoff`

`FlowContext.partner_handoff()` 会将授权对象包装为一个 `PartnerHandoff` ask。该 ask 是一条路由指令，而非可见内容。Textual 会话驱动负责解析并构建目标服务，再调用核心层投递逻辑，绝不会在任何组件或日志中渲染该 URL。

### `PartnerAuthorizationResult`

消费方返回以下结构：

```python
PartnerAuthorizationResult(
    service_id="consumer",
    authenticated=True,
    label="Consumer session saved",
    detail="Open Consumer to continue.",
)
```

`label` 和 `detail` 面向用户展示，不得包含任何 URL、令牌、Cookie、凭据或响应体。当 `authenticated` 为 `True` 时，结果对象的布尔值为真。

## 生命周期与职责边界

该流程由三个职责明确且高度受限的角色共同承担。

### 生产方

生产方应：

1. 仅调用自身的 API 创建合作方授权；
2. 校验并对返回的目标地址进行分类；
3. 使用显式的源服务 ID 和目标服务 ID 构造 `PartnerAuthorization`；
4. `yield ctx.partner_handoff(authorization)`；
5. 仅处理非敏感的返回结果。

生产方不得打开目标 URL、读取 `.url`、实例化消费方、导入消费方包，也不得缓存该 URL 用于重试。

```python
from ...core.partner import PartnerAuthorization, PartnerAuthorizationResult


def _open_partner(self, ctx, client):
    # The producer API, not the consumer, mints this URL.
    target = client.create_partner_authorization()
    authorization = PartnerAuthorization(
        target,
        source_service_id=self.ID,
        service_id="consumer",
        provider="Consumer",
    )

    result = yield ctx.partner_handoff(authorization)
    if isinstance(result, PartnerAuthorizationResult) and result.authenticated:
        ctx.log(result.label or "Consumer session saved", "ok")
    elif isinstance(result, PartnerAuthorizationResult):
        ctx.error(result.detail or result.label or "partner authorization failed")
    else:
        ctx.error("partner authorization returned no result")
```

如果返回的链接只是一个普通的外部浏览器登录页，应使用独立模型处理。不要把所有外部 URL 都包装成 `PartnerAuthorization`：那样会错误地宣称存在对应的已安装消费方及进程内兑换流程。

### 核心层

会话驱动会校验 `source_service_id` 是否为当前活跃的生产方，通过服务注册表解析 `service_id`，并构建一个新的消费方实例。随后 `deliver_partner_authorization()` 会校验：

- 接收方的 `ID` 与 `service_id` 精确匹配；
- 接收方显式接受该 `source_service_id`；
- 接收方返回 `PartnerAuthorizationResult`。

投递过程是同步的。其 `finally` 块会在成功、拒绝或发生异常（包括消费方尚未读取 URL 就发生的失败）时一律丢弃该 URL。投递完成后，核心层会清除消费方的账号状态提示缓存，但不会解析、存储或刷新由此产生的服务会话。

生产方与消费方服务应统一通过 Flow 交接完成交互。直接调用 `deliver_partner_authorization()` 仅适用于核心层驱动和定向单元测试，不得用作服务间互相导入的捷径。

### 消费方

消费方通过白名单显式声明接受的来源，并实现接收钩子：

```python
from ...core.partner import (
    PartnerAuthorization,
    PartnerAuthorizationError,
    PartnerAuthorizationResult,
)
from ...core.service import Service


class ConsumerError(RuntimeError):
    pass


class Consumer(Service):
    ID = "consumer"
    PARTNER_AUTHORIZATION_SOURCES = ("producer",)

    def consume_partner_authorization(
        self, authorization: PartnerAuthorization
    ) -> PartnerAuthorizationResult:
        if self.login_method() != "partner":
            return PartnerAuthorizationResult(
                self.ID,
                False,
                label="Consumer is set to account login",
            )
        if (
            authorization.service_id != self.ID
            or not self.accepts_partner_authorization(
                authorization.source_service_id
            )
        ):
            return PartnerAuthorizationResult(
                self.ID,
                False,
                label="Consumer refused the partner authorization",
            )

        try:
            # This is the only read. It claims and erases the contract URL.
            self.api.exchange_partner_url(authorization.url)
        except (ConsumerError, PartnerAuthorizationError) as exc:
            return PartnerAuthorizationResult(
                self.ID,
                False,
                label="Consumer partner sign-in failed",
                detail=str(exc),
            )

        return PartnerAuthorizationResult(
            self.ID,
            True,
            label="Consumer session saved",
        )
```

在发送凭据或令牌之前，消费方必须校验 URL 的协议方案、主机权限（authority）、重定向目标及预期载荷。通用契约仅保证 URL 为 HTTPS；特定提供方的主机白名单策略应由消费方自行实现。

认领 `.url` 后应立即完成兑换，并仅通过消费方自身的 `TokenStore` 和 Cookie 配置保存最终会话。消费方不得：

- 调用生产方的授权接口；
- 导入生产方服务或 API；
- 将一次性 URL 写入令牌、Cookie、设置、缓存、日志或异常信息；
- 将生产方的刷新令牌（refresh token）当作消费方的刷新令牌使用；
- 将该 URL 留存在队列、回调、任务记录或重试文件中；
- 在交接失败后自动回退到账号登录、匿名登录或浏览器登录，除非用户已显式选择了对应的独立登录方式。

## 多登录方式

消费方也可以同时支持自身的账号密码登录。应将其暴露为显式的服务设置，而非自动回退机制：

```python
Setting(
    key="login_method",
    label="Login method",
    kind="choice",
    options=[
        Option("partner", "Partner SSO"),
        Option("account", "Account - email and password"),
    ],
    default="partner",
    resets_session=True,
)
```

`resets_session=True` 会在切换设置时退出当前会话。服务还应为缓存状态标记所属的登录方式，拒绝加载来自另一认证域的令牌。合作方 Cookie 与账号令牌在格式上可能十分相似，但在彼此的租户体系中互相无效。

选择账号模式时，`consume_partner_authorization()` 应在不读取 `.url` 的前提下返回非敏感的拒绝结果，随后由核心层丢弃该 URL。选择合作方模式时，如果合作方会话缺失或过期，应引导用户回到生产方流程重新授权，而不是静默要求输入账号凭据。

## 参考实现

一组生产方/消费方实现应遵循以下模式：

- 生产方仅调用自身的授权 API，并使用显式的源服务 ID 和目标服务 ID 构造契约；
- 核心层校验白名单并在内存中路由这次一次性交接；
- 消费方仅认领一次 URL，仅跟随受信任的主机跳转，并将状态存入自己的令牌/Cookie 命名空间；
- 双方互不读取对方的凭据，也不调用对方的 API；
- 账号模式与合作方模式保持为相互隔离的认证域。

参考文件：

- `src/unidl/core/partner.py`
- `src/unidl/core/flow.py`（`PartnerHandoff` 与 `partner_handoff`）
- `src/unidl/tui/session.py`（`route_partner_authorization`）
- `src/unidl/services/<producer>/`（生产方）
- `src/unidl/services/<consumer>/`（消费方）

## 无头模式与测试

`AutoPresenter` 会刻意拒绝 `PartnerHandoff`：它没有服务注册表、配置或目标服务构建器，假装投递只会绕过契约校验。无头集成 presenter 必须提供相同的解析器，并使用构建出的接收方调用 `deliver_partner_authorization()`。

消费方的定向测试可以直接调用核心层投递函数：

```python
result = deliver_partner_authorization(authorization, receiver)
assert result.authenticated
assert authorization.claimed
```

测试应覆盖以下全部属性：

- 拒绝非 HTTPS URL 和无效的服务 ID；
- `repr()` 和错误信息不会暴露 URL；
- URL 仅限单次使用、支持过期且不可 pickle；
- 目标服务不匹配或生产方不受信任时，在消费前即予以拒绝；
- 所有成功与失败路径都会擦除 URL；
- 生产方发出正确的源/目标契约，且仅调用自身授权请求一次；
- 消费方处于其他登录模式时，在不读取 URL 的情况下拒绝交接；
- 消费方仅持久化最终状态，且文件权限为仅所有者可读写；
- 账号刷新令牌与合作方刷新令牌不可跨认证域复用；
- 直播、点播与许可证流量均使用当前所选登录方式对应的会话。

将项目冒烟测试与合作方服务的定向测试一同运行：

```bash
python -m pytest -q
```

测试夹具应使用占位 URL 和令牌。在线验证（live checks）可以汇报计数、类型和成功状态，但严禁打印授权 URL、令牌、Cookie、签名 manifest、许可证响应或内容密钥。

## 接入检查清单

- [ ] 仅由生产方自身调用其授权接口。
- [ ] 目标分类逻辑明确且遵循默认拒绝（fail-closed）原则。
- [ ] 契约中明确指定了已注册的源服务 ID 和目标服务 ID。
- [ ] 生产方通过 `yield ctx.partner_handoff()` 交接，且从不读取 `.url`。
- [ ] 消费方声明了 `PARTNER_AUTHORIZATION_SOURCES`。
- [ ] 消费方在读取 `.url` 之前先校验当前登录模式与目标服务。
- [ ] 消费方仅认领一次 `.url`，并同步完成兑换。
- [ ] 重定向与主机校验针对提供方专门编写，且默认拒绝。
- [ ] 仅将最终消费方会话状态存入消费方自有的令牌/Cookie 存储中。
- [ ] 返回结果与异常中不包含任何敏感值。
- [ ] 账号登录为显式独立选项且认证域隔离，不作为失败回退路径。
- [ ] 核心契约、生产方及消费方的夹具测试全部通过。
