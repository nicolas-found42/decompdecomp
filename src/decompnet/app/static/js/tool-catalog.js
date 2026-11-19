/* Tool schema cards shared by transcripts, MCP setup and tool controls. */
const TOOL_HELP = {
  project_info: "See the project, its progress and the tools it can build with.",
  list_functions: "Find pieces of game code by their progress.",
  claim_function: "Take a piece of work so two agents avoid doing it at the same time.",
  get_function: "Read the original code and any saved work on that function.",
  release_function: "Return a task so another agent can pick it up.",
  compile_and_diff: "Build the agent's code and check how closely it matches the game.",
  last_diff: "See where the agent's last attempt differs from the game.",
  read_data: "Read values from the original game that help explain its code.",
  get_class: "See groups of related functions and data in the game.",
  guess_struct: "Work out how the game groups values together in memory.",
  get_context: "Read definitions shared by the functions in the project.",
  edit_context: "Update shared definitions and check that existing matches still pass.",
  split_function: "Find separate functions that the original analysis treated as one.",
  join_functions: "Put a function back together when the analysis split it into pieces.",
  report_boundary: "Show where a function ends and explain the bytes that follow it.",
  rename_symbol: "Replace a placeholder name with a meaningful one.",
  add_note: "Leave what the next agent needs to know about a function.",
  search_notes: "Find what agents learned in earlier attempts.",
  share_knowledge: "Share a finding after the app or another model checks it.",
  find_similar: "Find a matching function that can guide this attempt.",
  guide: "Read the app's instructions for working on this game.",
  redraft: "Create a new starting point for a function using current definitions.",
  compiler_map: "See which compilers build different parts of the game.",
  try_compiler: "Try building saved work with another compiler.",
  compare_compilers: "Try the same work with several compilers and compare results.",
  search_flags: "Try different build options with a compiler.",
  suggest: "Report an improvement for agents or the app to check.",
  apply_compiler_settings: "Use different build settings after the app verifies an exact match.",
  claim_readable: "Choose verified code to make easier to read.",
  make_readable: "Improve names and explanations while keeping a byte match.",
  tidy_done: "Record what was cleaned up in a finished source file.",
  read_file: "Read code or definitions in a project file.",
  search_code: "Find related code in the project's own files.",
  edit_file: "Update a source file through the project's checks.",
  file_status: "See which parts of a source file already match.",
  split_file: "Move code into a separate source file and check the build.",
  standins_for: "Find temporary code that helps the project build.",
  pe_imports: "See which Windows library functions the game uses.",
  pe_data_refs: "Find where Windows code reads or writes shared data.",
  ps2_decompile: "Turn original PlayStation 2 instructions into a rough draft agents can read.",
  ps2_sdk_symbols: "Look for original names in PlayStation 2 libraries.",
  ps2_symbol_transfer: "Compare two PlayStation 2 programs to find names they share.",
  propose_sdk_name: "Leave a possible library name for other agents to check.",
  review_sdk_name: "Apply a supported library name or record why a suggestion was ruled out.",
  deep_permuter: "Try small changes to code that's close to matching. Save a better result when found.",
  verify_sdk_assembly: "Check whether original PlayStation 2 code set aside as assembly can be reproduced.",
  background_job: "See how a longer check is going, or stop a check this agent started."
};

function toolCatalog(tools, options = {}) {
  const catalog = document.createElement("div"); catalog.className = "tool-catalog transcript-tools" + (options.editable ? " tool-controls-catalog" : "");
  const heading = document.createElement("p"); heading.className = "small muted";
  heading.textContent = `${tools.length} tools`; catalog.append(heading);
  for (const item of tools) {
    const tool = item.function || item, schema = tool.input_schema || tool.inputSchema || tool.parameters || {};
    const card = document.createElement("details"); card.className = "transcript-tool";
    const summary = document.createElement("summary"), name = document.createElement("code"), description = document.createElement("p");
    name.textContent = tool.name; summary.append(name);
    description.textContent = Object.hasOwn(TOOL_HELP, tool.name) ? TOOL_HELP[tool.name] : tool.description || "No description provided.";
    if (options.editable) {
      const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.checked = item.enabled !== false;
      toggle.disabled = !!item.required || (!!options.helper && item.global_disabled);
      toggle.setAttribute('aria-label', `Enable ${tool.name}`);
      toggle.onclick = event => event.stopPropagation();
      toggle.onchange = () => options.onToggle(tool.name, toggle.checked, toggle);
      summary.append(toggle);
    }
    let usageDetail = null;
    if (item.calls != null) {
      const count = document.createElement('span'); count.className = 'small muted';
      count.textContent = `${item.calls.toLocaleString()}${options.editable ? ' calls' : ' recorded calls'}${item.required ? ' · required' : options.helper && item.global_disabled ? ' · disabled globally' : ''}`;
      summary.append(count);
      if (item.task_calls || item.schema_chars != null) {
        const detail = document.createElement('p'); detail.className = 'small muted';
        detail.textContent = [...Object.entries(item.task_calls || {}).map(([task,n])=>`${task}: ${n.toLocaleString()} calls`),
          item.schema_chars == null ? '' : `${item.schema_chars.toLocaleString()} tool-definition characters`].filter(Boolean).join(' · ');
        usageDetail = detail;
      }
    }
    card.append(summary, description);
    if (usageDetail) card.append(usageDetail);
    const technical = document.createElement("details"), technicalTitle = document.createElement("summary");
    technicalTitle.textContent = "Technical details";
    technical.append(technicalTitle);
    if (tool.description && description.textContent !== tool.description) {
      const note = document.createElement("p"); note.textContent = tool.description; technical.append(note);
    }
    const properties = schema.properties;
    if (properties && typeof properties === "object" && Object.keys(properties).length) {
      const list = document.createElement("dl"); list.className = "transcript-tool-params";
      for (const [key, value] of Object.entries(properties)) {
        const param = value && typeof value === "object" ? value : {};
        const term = document.createElement("dt"), detail = document.createElement("dd");
        term.textContent = `${key}${Array.isArray(schema.required) && schema.required.includes(key) ? " · required" : " · optional"}`;
        const type = Array.isArray(param.type) ? param.type.join(" / ") : param.type || (param.enum ? "enum" : "value");
        detail.textContent = [type, param.description, param.enum ? `Choices: ${JSON.stringify(param.enum)}` : "", Object.hasOwn(param, "default") ? `Default: ${JSON.stringify(param.default)}` : ""].filter(Boolean).join(" · ");
        list.append(term, detail);
      }
      technical.append(list);
    } else {
      const empty = document.createElement("p"); empty.className = "small muted";
      empty.textContent = "No inputs."; technical.append(empty);
    }
    card.append(technical);
    catalog.append(card);
  }
  return catalog;
}
