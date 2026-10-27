# frozen_string_literal: true

# Prism chunker: reads NUL-separated repo-relative paths on stdin, writes JSONL chunks.
# One chunk per method, plus class-body chunks for DSL calls (scopes, associations, callbacks).
require 'prism'
require 'json'

MAX_CHARS = Integer(ENV.fetch('CHUNK_MAX_CHARS', '1000'))
OVERLAP_LINES = 3

def emit(path, lines, first, last, name, kind)
  # Leading comments document the method, so pull them in.
  first -= 1 while kind == 'method' && first > 1 && lines[first - 2] =~ /\A\s*#/
  body = lines[(first - 1)..(last - 1)] || []
  windows(body, first).each_with_index do |(s, e, text), i|
    part = s == first && e == last ? '' : " (part #{i + 1})"
    header = "# #{path}:#{s}-#{e} #{name}#{part}"
    puts JSON.generate(path: path, start_line: s, end_line: e, name: name, kind: kind, text: "#{header}\n#{text}")
  end
end

# Split a line range into windows under MAX_CHARS, cutting only at line breaks.
def windows(body, first)
  out = []
  i = 0
  while i < body.size
    j = i
    size = 0
    while j < body.size && (j == i || size + body[j].size <= MAX_CHARS)
      size += body[j].size
      j += 1
    end
    out << [first + i, first + j - 1, body[i...j].join]
    break if j >= body.size

    i = [j - OVERLAP_LINES, i + 1].max
  end
  out
end

def contains_def?(node)
  node.compact_child_nodes.any? { |c| c.is_a?(Prism::DefNode) || contains_def?(c) }
end

CONTAINERS = [Prism::ClassNode, Prism::ModuleNode, Prism::SingletonClassNode].freeze

# Emit statements of a class/module/file body that are not defs or nested containers.
def emit_body(path, lines, statements, container_line, name)
  rows = []
  statements.each do |st|
    next if st.is_a?(Prism::DefNode) || CONTAINERS.any? { |k| st.is_a?(k) }

    s = st.location.start_line
    e = contains_def?(st) ? s : st.location.end_line
    rows.concat((s..e).to_a)
  end
  return if rows.empty?

  rows.unshift(container_line) if container_line
  # Runs separated only by blank or comment lines belong together.
  runs = []
  rows.uniq.sort.chunk_while { |a, b| b == a + 1 }.each do |run|
    gap = runs.any? ? ((runs[-1][1] + 1)...run.first) : nil
    if gap&.all? { |l| lines[l - 1].to_s.strip.empty? || lines[l - 1] =~ /\A\s*#/ }
      runs[-1][1] = run.last
    else
      runs << [run.first, run.last]
    end
  end
  runs.each do |s, e|
    next if lines[(s - 1)..(e - 1)].join.gsub(/\b(private|protected|public|end)\b/, '').strip.size < 20

    emit(path, lines, s, e, name, 'body')
  end
end

def walk(path, lines, node, nest, singleton)
  case node
  when Prism::ClassNode, Prism::ModuleNode
    nest += [node.constant_path.slice]
    stmts = node.body.is_a?(Prism::StatementsNode) ? node.body.body : []
    emit_body(path, lines, stmts, node.location.start_line, "#{nest.join('::')} (class body)")
    singleton = false
  when Prism::SingletonClassNode
    singleton = true
  when Prism::CallNode
    singleton = true if node.name == :class_methods && node.block
  when Prism::DefNode
    sep = node.receiver || singleton ? '.' : '#'
    owner = nest.empty? ? '' : nest.join('::')
    emit(path, lines, node.location.start_line, node.location.end_line, "#{owner}#{sep}#{node.name}", 'method')
    return
  end
  node.compact_child_nodes.each { |c| walk(path, lines, c, nest, singleton) }
end

$stdin.read.split("\0").each do |path|
  next if path.empty?

  begin
    source = File.read(path)
    result = Prism.parse(source)
    lines = source.lines
    top = result.value.statements.body
    emit_body(path, lines, top, nil, '(top level)')
    walk(path, lines, result.value, [], false)
  rescue StandardError => e
    warn "chunk_ruby: #{path}: #{e.class}: #{e.message}"
  end
end
