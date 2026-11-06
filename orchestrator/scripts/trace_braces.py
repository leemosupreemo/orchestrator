
import re
import sys

def trace_braces(file_path):
    with open(file_path, 'r') as f:
        content = f.read()
        
    # Remove strings
    content = re.sub(r'"([^"\\]|\\.)*"', '""', content)
    # Remove single line comments
    content = re.sub(r'//.*', '', content)
    # Remove multi-line comments
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    
    stack = []
    lines = content.split('\n')
    for line_num, line in enumerate(lines, 1):
        for char_num, char in enumerate(line, 1):
            if char == '{':
                stack.append((line_num, char_num))
            elif char == '}':
                if not stack:
                    print(f"Extra closing brace at line {line_num}, col {char_num}")
                else:
                    stack.pop()
    
    if stack:
        print(f"Unclosed braces at:")
        for ln, cn in stack:
            print(f"Line {ln}, col {cn}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        trace_braces(sys.argv[1])
