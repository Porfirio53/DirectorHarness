export interface RuntimeSettings {
  writerModel: string;
  writerBaseUrl: string;
  writerApiKey: string;
  actorModel: string;
  actorBaseUrl: string;
  actorApiKey: string;
  actorApiFormat: string;
  ohBin: string;
  openharnessSrc: string;
  directorLogPath: string;
}

interface SettingField {
  key: keyof RuntimeSettings;
  label: string;
  environmentVariable: string;
  description: string;
  placeholder: string;
  type?: "password";
}

interface SettingGroup {
  title: string;
  description: string;
  fields: SettingField[];
}

const settingGroups: SettingGroup[] = [
  {
    title: "编剧模型",
    description: "配置 Writer Harness 的剧本生成、完整性判断与评分模型。",
    fields: [
      { key: "writerModel", label: "模型名称", environmentVariable: "WRITER_MODEL", description: "Writer Harness 用于剧本生成、完整性判断和评分的模型名称。", placeholder: "例如：模型 ID" },
      { key: "writerBaseUrl", label: "接口地址", environmentVariable: "WRITER_BASE_URL", description: "编剧模型 API 的服务地址，应包含协议与必要路径。", placeholder: "https://api.example.com/v1" },
      { key: "writerApiKey", label: "API 密钥", environmentVariable: "WRITER_API_KEY", description: "用于认证编剧模型 API 的密钥；页面仅在当前浏览器中持久化。", placeholder: "请输入 API Key", type: "password" },
    ],
  },
  {
    title: "演员模型",
    description: "配置 OpenHarness 在真实执行阶段使用的模型与接口格式。",
    fields: [
      { key: "actorModel", label: "模型名称", environmentVariable: "ACTOR_MODEL", description: "OpenHarness 在真实执行阶段调用的模型名称。", placeholder: "例如：模型 ID" },
      { key: "actorBaseUrl", label: "接口地址", environmentVariable: "ACTOR_BASE_URL", description: "演员模型 API 的服务地址，应包含协议与必要路径。", placeholder: "https://api.example.com/v1" },
      { key: "actorApiKey", label: "API 密钥", environmentVariable: "ACTOR_API_KEY", description: "用于认证演员模型 API 的密钥；页面仅在当前浏览器中持久化。", placeholder: "请输入 API Key", type: "password" },
      { key: "actorApiFormat", label: "API 格式", environmentVariable: "ACTOR_API_FORMAT", description: "演员模型接口的请求格式，例如 openai 或 anthropic。", placeholder: "例如：openai" },
    ],
  },
  {
    title: "导演 Harness",
    description: "配置 Director Harness 的事件审计日志；MCP 目录由项目内默认目录自动加载。",
    fields: [
      { key: "directorLogPath", label: "事件日志路径", environmentVariable: "DIRECTOR_LOG_PATH", description: "Director Harness 工具检查事件的 JSONL 输出路径。留空时默认写入项目 logs/director-events.jsonl。", placeholder: "例如：logs/director-events.jsonl" },
    ],
  },
  {
    title: "其他设置",
    description: "配置本地 OpenHarness 命令与源码目录。",
    fields: [
      { key: "ohBin", label: "OpenHarness 可执行文件", environmentVariable: "OH_BIN", description: "oh 或 oh.exe 的绝对路径，或系统 PATH 中可直接调用的命令名。", placeholder: "例如：C:\\tools\\oh.exe" },
      { key: "openharnessSrc", label: "OpenHarness 源码目录", environmentVariable: "OPENHARNESS_SRC", description: "OpenHarness 源码的绝对路径，执行时传递给命令行入口。", placeholder: "例如：G:\\OpenHarness\\src" },
    ],
  },
];

export function SettingsPage({ settings, onChange }: { settings: RuntimeSettings; onChange: (key: keyof RuntimeSettings, value: string) => void }) {
  const configuredCount = settingGroups.flatMap((group) => group.fields).filter((field) => settings[field.key].trim()).length;
  return (
    <section className="settings-page">
      <header className="settings-hero">
        <span className="eyebrow">Runtime Configuration</span>
        <h1>运行设置</h1>
        <p>填写后的值会覆盖运行服务的同名环境变量；留空时继续使用系统设置。所有设置会保留在当前浏览器中。</p>
        <span className="settings-hero__status">已配置 {configuredCount} / 10 项覆盖参数</span>
      </header>
      <div className="settings-card">
        <div className="settings-card__header">
          <div>
            <h2>命令行环境变量覆盖</h2>
            <p>对应在线执行命令中的 Writer、Actor 与 OpenHarness 参数。</p>
          </div>
          <span className="settings-card__hint">非空优先</span>
        </div>
        <div className="settings-groups">
          {settingGroups.map((group) => (
            <section className="settings-group" key={group.title}>
              <div className="settings-group__header">
                <h3>{group.title}</h3>
                <p>{group.description}</p>
              </div>
              <div className="settings-fields">
                {group.fields.map((field) => (
                  <label className="settings-field" key={field.key}>
                    <span className="settings-field__header">
                      <strong>{field.label}</strong>
                      <code>{field.environmentVariable}</code>
                    </span>
                    <input type={field.type || "text"} value={settings[field.key]} placeholder={field.placeholder} onChange={(event) => onChange(field.key, event.target.value)} autoComplete={field.type ? "off" : undefined} />
                    <span className="settings-field__description">{field.description}</span>
                  </label>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </section>
  );
}
