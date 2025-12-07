"""
文件复制工具 - 在主机和Docker容器之间双向复制文件
"""
import os
import tarfile
import io
from pathlib import Path
from typing import List, Dict, Any
from tqdm import tqdm
import docker

## -!- START TOOL DEFINITION -!-
TOOL_NAME = "file_copy_container"
TOOL_DESCRIPTION = "在主机和Docker容器之间双向复制文件的工具。支持批量复制多个文件，每个文件可独立指定目标路径。支持进度条显示和错误处理。"
TOOL_FUNCTIONS = ["copy_files_from_host_to_container", "copy_files_from_container_to_host"]
TOOL_PARAMETERS = [
    [
        {"host_file_paths": "主机源文件路径列表，每个元素为绝对路径字符串"},
        {"container_dest_paths": "容器内目标路径列表，与host_file_paths一一对应，包含完整路径和文件名"}
    ],
    [
        {"container_file_paths": "容器内源文件路径列表，每个元素为绝对路径字符串"},
        {"host_dest_paths": "主机目标路径列表，与container_file_paths一一对应，包含完整路径和文件名"}
    ]
]
## -!- END TOOL DEFINITION -!-


def _get_container(container_name: str):
    """获取Docker容器客户端"""
    client = docker.from_env()
    try:
        container = client.containers.get(container_name)
        return container
    except docker.errors.NotFound:
        raise Exception(f"容器 '{container_name}' 不存在")
    except Exception as e:
        raise Exception(f"连接Docker失败: {str(e)}")


def _create_tar_for_file(file_path: str) -> io.BytesIO:
    """为单个文件创建tar归档"""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    if not file_path.is_file():
        raise ValueError(f"路径不是文件: {file_path}")

    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        tar.add(file_path, arcname=file_path.name)

    tar_stream.seek(0)
    return tar_stream


def copy_files_from_host_to_container(
    host_file_paths: List[str],
    container_dest_paths: List[str]
) -> Dict[str, Any]:
    """
    将文件从主机复制到Docker容器

    参数:
        host_file_paths: 主机源文件路径列表（绝对路径）
        container_dest_paths: 容器内目标路径列表，与源文件一一对应

    返回:
        包含成功和失败文件信息的字典
    """
    # 验证参数
    if len(host_file_paths) != len(container_dest_paths):
        return {
            "status": "error",
            "message": "源文件路径列表和目标路径列表长度必须相同"
        }

    if not host_file_paths:
        return {
            "status": "success",
            "message": "没有文件需要复制",
            "success_count": 0,
            "fail_count": 0,
            "results": []
        }

    # 读取配置获取容器名称
    import json
    config_path = Path("/data0/MuLi/config.json")
    container_name = "ai_shell_container"  # 默认值
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                container_name = config.get("tools_api_config", {}).get("shell_for_ai", {}).get("container_name", "ai_shell_container")
        except:
            pass

    try:
        container = _get_container(container_name)
    except Exception as e:
        return {
            "status": "error",
            "message": f"获取容器失败: {str(e)}"
        }

    results = []
    success_count = 0
    fail_count = 0

    # 总体进度条
    with tqdm(total=len(host_file_paths), desc="总体进度") as pbar_total:
        for idx, (host_path, container_path) in enumerate(zip(host_file_paths, container_dest_paths)):
            host_path = Path(host_path)
            file_result = {
                "source": str(host_path),
                "destination": container_path,
                "status": "pending"
            }

            try:
                # 验证源文件
                if not host_path.exists():
                    raise FileNotFoundError(f"文件不存在: {host_path}")

                if not host_path.is_file():
                    raise ValueError(f"路径不是文件: {host_path}")

                # 创建tar归档
                tar_stream = _create_tar_for_file(str(host_path))

                # 获取容器内目标目录
                container_dir = str(Path(container_path).parent)
                container_name_in_container = Path(container_path).name

                # 提取目录路径（确保tar归档中的文件名匹配）
                tar_stream.seek(0)
                with tarfile.open(fileobj=tar_stream, mode='r') as tar:
                    members = tar.getmembers()
                    if members:
                        members[0].name = container_name_in_container

                # 重新创建tar流
                tar_stream = _create_tar_for_file(str(host_path))
                tar_stream.seek(0)

                # 写入容器
                success = container.put_archive(path=container_dir, data=tar_stream.read())

                if success:
                    file_result["status"] = "success"
                    success_count += 1
                else:
                    file_result["status"] = "failed"
                    file_result["error"] = "Docker API返回失败"
                    fail_count += 1

            except Exception as e:
                file_result["status"] = "failed"
                file_result["error"] = str(e)
                fail_count += 1

            results.append(file_result)
            pbar_total.update(1)

    return {
        "status": "completed",
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results
    }


def copy_files_from_container_to_host(
    container_file_paths: List[str],
    host_dest_paths: List[str]
) -> Dict[str, Any]:
    """
    将文件从Docker容器复制到主机

    参数:
        container_file_paths: 容器内源文件路径列表（绝对路径）
        host_dest_paths: 主机目标路径列表，与源文件一一对应

    返回:
        包含成功和失败文件信息的字典
    """
    # 验证参数
    if len(container_file_paths) != len(host_dest_paths):
        return {
            "status": "error",
            "message": "源文件路径列表和目标路径列表长度必须相同"
        }

    if not container_file_paths:
        return {
            "status": "success",
            "message": "没有文件需要复制",
            "success_count": 0,
            "fail_count": 0,
            "results": []
        }

    # 读取配置
    import json
    config_path = Path("/data0/MuLi/config.json")
    container_name = "ai_shell_container"
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                container_name = config.get("tools_api_config", {}).get("shell_for_ai", {}).get("container_name", "ai_shell_container")
        except:
            pass

    try:
        container = _get_container(container_name)
    except Exception as e:
        return {
            "status": "error",
            "message": f"获取容器失败: {str(e)}"
        }

    results = []
    success_count = 0
    fail_count = 0

    # 总体进度条
    with tqdm(total=len(container_file_paths), desc="总体进度") as pbar_total:
        for container_path, host_path in zip(container_file_paths, host_dest_paths):
            host_path = Path(host_path)
            file_result = {
                "source": container_path,
                "destination": str(host_path),
                "status": "pending"
            }

            try:
                # 确保主机目标目录存在
                host_path.parent.mkdir(parents=True, exist_ok=True)

                # 从容器获取文件
                stream, stat = container.get_archive(container_path)

                if not stream:
                    raise FileNotFoundError(f"容器内文件不存在: {container_path}")

                # 将tar流写入临时文件
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                    tmp_path = tmp_file.name
                    for chunk in stream:
                        tmp_file.write(chunk)

                # 解压到目标位置
                with tarfile.open(tmp_path, 'r') as tar:
                    # 获取tar中的文件
                    members = tar.getmembers()
                    if not members:
                        raise ValueError(f"tar归档为空: {container_path}")

                    # 重命名为目标文件名
                    target_name = host_path.name
                    members[0].name = target_name

                    # 提取到目标目录
                    tar.extract(members[0], path=str(host_path.parent))

                    # 设置权限（如果可能）
                    extracted_path = host_path.parent / target_name
                    if hasattr(members[0], 'mode'):
                        try:
                            os.chmod(extracted_path, members[0].mode)
                        except:
                            pass

                # 清理临时文件
                Path(tmp_path).unlink(missing_ok=True)

                file_result["status"] = "success"
                success_count += 1

            except Exception as e:
                file_result["status"] = "failed"
                file_result["error"] = str(e)
                fail_count += 1

            results.append(file_result)
            pbar_total.update(1)

    return {
        "status": "completed",
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results
    }
