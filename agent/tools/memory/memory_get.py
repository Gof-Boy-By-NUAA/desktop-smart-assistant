"""
Memory get tool

Allows agents to read specific sections from memory files
"""

import os

from agent.tools.base_tool import BaseTool


class MemoryGetTool(BaseTool):
    """Tool for reading memory file contents"""
    
    name: str = "memory_get"
    description: str = (
        "Read specific content from memory files. "
        "Use this to get full context from a memory file or specific line range."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the memory file (e.g. 'MEMORY.md', 'memory/2026-01-01.md')"
            },
            "memory_id": {
                "type": "string",
                "description": "Governed memory ID returned by memory_search or memory_write"
            },
            "start_line": {
                "type": "integer",
                "description": "Starting line number (optional, default: 1)",
                "default": 1
            },
            "num_lines": {
                "type": "integer",
                "description": "Number of lines to read (optional, reads all if not specified)"
            }
        },
        "required": []
    }
    
    def __init__(self, memory_manager, identity=None, session_id=None):
        """
        Initialize memory get tool
        
        Args:
            memory_manager: MemoryManager instance
        """
        super().__init__()
        self.memory_manager = memory_manager
        self.session_id = session_id
        if identity is None:
            from agent.memory.governance import IdentityContext

            identity = IdentityContext(
                tenant_id=memory_manager.config.tenant_id,
                actor_user_id="local-user",
                roles=frozenset(),
                trace_id="trace-memory-get",
                auth_source="smart-assistant-local-runtime",
            )
        self.identity = identity

    def execute(self, args: dict):
        """
        Execute memory file read
        
        Args:
            args: Dictionary with path, start_line, num_lines
            
        Returns:
            ToolResult with file content
        """
        from agent.tools.base_tool import ToolResult
        
        path = args.get("path")
        memory_id = args.get("memory_id")
        start_line = args.get("start_line", 1)
        num_lines = args.get("num_lines")

        if isinstance(path, str) and path.startswith("governed://"):
            memory_id = path[len("governed://"):]

        if memory_id:
            try:
                record = self.memory_manager.get_governed_memory(
                    self.identity,
                    memory_id,
                    session_id=self.session_id,
                )
                return ToolResult.success(
                    "\n".join(
                        [
                            f"Memory ID: {record.memory_id}",
                            f"Version: {record.version}",
                            f"Scope: {record.scope.value}",
                            f"Sensitivity: {record.sensitivity.value}",
                            f"Source: {record.source_ref}",
                            f"Content hash: {record.content_hash}",
                            "",
                            record.content,
                        ]
                    )
                )
            except Exception as error:
                return ToolResult.fail(f"Error reading governed memory: {error}")

        if not isinstance(path, str) or not path:
            return ToolResult.fail("Error: path or memory_id parameter is required")
        
        try:
            workspace_dir = self.memory_manager.config.get_workspace()
            workspace_resolved = workspace_dir.resolve()

            from agent.tools.utils.governed_memory import (
                is_governed_private_path,
                is_machine_managed_knowledge_path,
            )

            # 先按调用方原始路径判定，避免大小写或 ./ 前缀绕过后被错误补到 memory/。
            requested_file = workspace_dir / path
            if is_machine_managed_knowledge_path(
                str(requested_file), str(workspace_resolved)
            ):
                return ToolResult.fail(
                    "Error: governed knowledge must be accessed through "
                    "knowledge_search or knowledge_get"
                )
            
            # Auto-prepend memory/ if not present and not absolute path
            # MEMORY.md 保留在工作区根目录。
            if (
                not path.startswith('memory/')
                and not os.path.isabs(path)
                and path != 'MEMORY.md'
            ):
                path = f'memory/{path}'
            
            file_path = (workspace_dir / path).resolve()

            # Use os.path.realpath + os.sep for cross-platform path validation.
            # str(Path).startswith(str + '/') fails on Windows where Path uses
            # backslashes — see MemoryService._resolve_path for the same pattern.
            real_file = os.path.realpath(str(file_path))
            real_workspace = os.path.realpath(str(workspace_resolved))
            if real_file != real_workspace and not real_file.startswith(real_workspace + os.sep):
                return ToolResult.fail(f"Error: Access denied: path outside workspace")

            if is_governed_private_path(real_file, real_workspace):
                return ToolResult.fail(
                    "Error: governed private data must be read through its dedicated identifier"
                )
            
            if not file_path.exists():
                return ToolResult.fail(f"Error: File not found: {path}")
            
            content = file_path.read_text(encoding='utf-8')
            lines = content.split('\n')
            
            # Handle line range
            if start_line < 1:
                start_line = 1
            
            start_idx = start_line - 1
            
            if num_lines:
                end_idx = start_idx + num_lines
                selected_lines = lines[start_idx:end_idx]
            else:
                selected_lines = lines[start_idx:]
            
            result = '\n'.join(selected_lines)
            
            # Add metadata
            total_lines = len(lines)
            shown_lines = len(selected_lines)
            
            output = [
                f"File: {path}",
                f"Lines: {start_line}-{start_line + shown_lines - 1} (total: {total_lines})",
                "",
                result
            ]
            
            return ToolResult.success('\n'.join(output))
            
        except Exception as e:
            return ToolResult.fail(f"Error reading memory file: {str(e)}")
