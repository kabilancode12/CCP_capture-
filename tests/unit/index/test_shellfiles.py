def test_paths_after_a_relative_cd_are_inside_that_folder_and_a_trailing_slash_is_not_a_second_file() -> (
    None
):
    from ccp.index.shellfiles import files_changed_by

    command = "mkdir -p src/ccp/server && cd src/ccp/server && touch __init__.py && cat > plugins.py <<'EOF'\nx\nEOF\n"
    paths = [f.path for f in files_changed_by(command)]
    assert "src/ccp/server/__init__.py" in paths and "src/ccp/server/plugins.py" in paths
    assert "__init__.py" not in paths and "src/ccp/server" in paths
    both = files_changed_by("mkdir -p ~/.config/u && touch ~/.config/u/ && echo x > ~/.config/u")
    assert [f.path for f in both].count("~/.config/u") == 1
    absolute = files_changed_by("cd /home/me/proj && cat > a.py <<'EOF'\nx\nEOF\n")
    assert [f.path for f in absolute] == ["a.py"]  # an absolute cd is left as it was
