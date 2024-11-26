from flask import Flask, request, jsonify, Response
import subprocess
import os
import json
import yaml
import time
import shutil
import zipfile
from apscheduler.schedulers.background import BackgroundScheduler
import re
import requests

app = Flask(__name__)

# 环境变量：集群域名
CLUSTER_DOMAIN = os.getenv('CLUSTER_DOMAIN')
# 环境变量：镜像仓库地址
REGISTRY_URL = os.getenv('REGISTRY_URL')
# 环境变量：镜像仓库用户名
REGISTRY_USER = os.getenv('REGISTRY_USER')
# 环境变量：镜像仓库密码
REGISTRY_PASS = os.getenv('REGISTRY_PASS')
# 环境变量：文件保存路径
SAVE_PATH = os.getenv('SAVE_PATH')
# 环境变量：资源利用比例
RESOURCE_THRESHOLD = os.getenv('RESOURCE_THRESHOLD') or '70'

# 辅助函数：执行shell命令
def run_command(command):
    try:
        subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return None
    except subprocess.CalledProcessError as e:
        print("Error executing command: " + e.stderr.decode().strip())
        return e.stderr.decode().strip()

def upload_deploy_helper(file_path, namespace, appname, images):
    for image in images:
        image['path'] = os.path.join(file_path, image['path'].split('/')[-1])
    with open(os.path.join(file_path, 'app.yaml'), 'r') as file:
        yaml_content = file.read()

    new_yaml_contents = []
    for single_yaml in yaml.safe_load_all(yaml_content):
        if 'kind' in single_yaml and single_yaml['kind'] == 'Deployment':
            if 'spec' in single_yaml and 'template' in single_yaml['spec'] and 'spec' in single_yaml['spec']['template']:
                if 'containers' in single_yaml['spec']['template']['spec']:
                    for container_index in range(len(single_yaml['spec']['template']['spec']['containers'])):
                        container = single_yaml['spec']['template']['spec']['containers'][container_index]
                        if 'image' in container:
                            if not '/' in container['image']:
                                container['image'] = 'library/' + container['image']
                            if not ':' in container['image']:
                                container['image'] = container['image'] + ':latest'
        new_yaml_contents.append(single_yaml)
    new_yaml_content = yaml.dump_all(new_yaml_contents)


    print('deployAppWithImage, appname:', appname, 'namespace:', namespace, flush=True)

    # 加载和推送镜像
    for image in images:
        name = image['name'].strip()
        path = image['path']

        # 登录镜像仓库
        err = run_command('docker login -u admin -p passw0rd sealos.hub:5000')
        if err:
            return jsonify({'error': 'Failed to login, ' + err}), 500

        # 加载镜像
        err = run_command('docker load -i ' + path)
        if err:
            return jsonify({'error': 'Failed to load image, ' + err}), 500
        # 替换域名并推送镜像
        parts = name.split('/')
        if len(parts) == 3:
            new_name = 'sealos.hub:5000/' + '/'.join(parts[1:])
        elif len(parts) == 1:
            new_name = 'sealos.hub:5000/library/' + name
        elif len(parts) == 2:
            new_name = 'sealos.hub:5000/' + name
        else:
            return jsonify({'error': 'Invalid image name: ' + name}), 400
        err = run_command('docker tag ' + name + ' ' + new_name)
        if err:
            return jsonify({'error': 'Failed to tag image, ' + err}), 500
        err = run_command('docker push ' + new_name)
        if err:
            return jsonify({'error': 'Failed to push image, ' + err}), 500

    # 替换yaml中的CLUSTER_DOMAIN
    new_yaml_content = new_yaml_content.replace('CLUSTER_DOMAIN', CLUSTER_DOMAIN)
    with open('temp.yaml', 'w') as file:
        file.write(new_yaml_content)

    # 调用kubectl创建命名空间
    create_namespace_command = 'kubectl create namespace ' + namespace + ' --kubeconfig=/etc/kubernetes/admin.conf'
    err = run_command(create_namespace_command)

    if err:
        if 'already exists' not in err:
            return jsonify({'error': 'Failed to create namespace, ' + err}), 500

    # 调用kubectl部署应用
    apply_command = 'kubectl apply -n ' + namespace + ' --kubeconfig=/etc/kubernetes/admin.conf -f temp.yaml'
    err = run_command(apply_command)

    if err:
        return jsonify({'error': 'Failed to apply application, ' + err}), 500

    # 返回成功响应
    detail_url = 'http://' + CLUSTER_DOMAIN + ':32293/app/detail'
    return jsonify({'message': 'Application deployed successfully', 'url': detail_url}), 200

# API端点：导出应用程序
@app.route('/api/exportApp', methods=['POST'])
def export_app():
    # 获取请求参数 应用编排yaml，应用镜像列表，应用名称，命名空间
    yaml_content = request.json.get('yaml')
    if not yaml_content:
        return jsonify({'error': 'YAML is required'}), 400
    images = request.json.get('images')
    if not images:
        return jsonify({'error': 'Images are required'}), 400
    appname = request.args.get('appname')
    if not appname:
        return jsonify({'error': 'Appname is required'}), 400
    namespace = request.args.get('namespace')
    if not namespace:
        return jsonify({'error': 'Namespace is required'}), 400

    print('exportApp, appname:', request.args.get('appname'), 'namespace:', request.args.get('namespace'), flush=True)

    workdir = os.path.join(SAVE_PATH, namespace, appname)
    
    if os.path.exists(workdir):
        os.system('rm -rf ' + workdir)
    os.makedirs(workdir)

    # 保存yaml文件至本地
    print('write yaml file to:', os.path.join(workdir, 'app.yaml'), flush=True)
    with open(os.path.join(workdir, 'app.yaml'), 'w') as file:
        file.write(yaml_content)

    # 检索yaml中的所有nodeport端口和对应的内部port
    nodeports = []
    for single_yaml in yaml.safe_load_all(yaml_content):
        if 'kind' in single_yaml and single_yaml['kind'] == 'Service':
            if 'spec' in single_yaml and 'type' in single_yaml['spec'] and single_yaml['spec']['type'] == 'NodePort':
                for port_index in range(len(single_yaml['spec']['ports'])):
                    nodeports.append({'internal_port': str(single_yaml['spec']['ports'][port_index]['port']), 'external_port': ''})
    print('nodeports:', nodeports, flush=True)

    image_pairs = []
    
    # 登录镜像仓库
    print('login to registry', flush=True)
    err = run_command('docker login -u admin -p passw0rd sealos.hub:5000')
    if err:
        return jsonify({'error': 'Failed to login, ' + err}), 500
    
    # 拉取镜像并保存到本地
    for image in images:
        name = image['name'].strip()
        print('pull image:', name, flush=True)
        image_file_name = name.replace('/', '_').replace(':', '_') + '.tar'
        path = os.path.join(workdir, image_file_name)
        image_pairs.append({'name': name, 'path': path})
        err = run_command('docker pull ' + name)
        if err:
            return jsonify({'error': 'Failed to pull image, ' + err}), 500
        print('save image:', name, flush=True)
        err = run_command('docker save ' + name + ' -o ' + path)
        if err:
            return jsonify({'error': 'Failed to save image, ' + err}), 500
    
    # 保存元数据信息
    metadata = {
        'name': appname,
        'namespace': namespace,
        'images': image_pairs,
        'nodeports': nodeports
    }
    with open(os.path.join(workdir, 'metadata.json'), 'w') as file:
        file.write(json.dumps(metadata))
    
    # 返回成功响应
    return jsonify({'message': 'Application exported successfully', 'path': workdir, 'url': 'http://' + CLUSTER_DOMAIN + ':5002/api/downloadApp?appname=' + appname + '&namespace=' + namespace}), 200

# API端点：打包并下载应用程序
@app.route('/api/downloadApp', methods=['GET'])
def download_app():
    # 获取请求参数
    appname = request.args.get('appname')
    if not appname:
        return jsonify({'error': 'Appname is required'}), 400
    namespace = request.args.get('namespace')
    if not namespace:
        return jsonify({'error': 'Namespace is required'}), 400

    print('downloadApp, appname:', appname, 'namespace:', namespace, flush=True)

    # 打包应用程序为zip文件
    workdir = os.path.join(SAVE_PATH, namespace, appname)
    zip_path = os.path.join(SAVE_PATH, namespace, appname + '.zip')
    shutil.make_archive(base_name=os.path.splitext(zip_path)[0], format='zip', root_dir=workdir)

    # 以流的形式返回文件
    def generate():
        with open(zip_path, 'rb') as file:
            while True:
                data = file.read(1024)
                if not data:
                    break
                yield data

    response = Response(generate(), content_type='application/zip')
    response.headers['Content-Disposition'] = 'attachment; filename=' + appname + '.zip'
    return response

# API端点：上传应用程序
@app.route('/api/uploadApp', methods=['POST'])
def upload_app():
    # 检查文件上传
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in the request'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected for uploading'}), 400

    # 检查并创建保存路径
    workdir = os.path.join(SAVE_PATH, 'temp')
    if not os.path.exists(workdir):
        os.makedirs(workdir)

    # 保存上传的zip文件
    zip_path = os.path.join(workdir, file.filename)
    file.save(zip_path)
    print('Saved file to:', zip_path, flush=True)

    # 解压上传的zip文件
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(workdir)
        print('Extracted zip file successfully.', flush=True)
    except zipfile.BadZipFile as e:
        return jsonify({'error': 'Failed to extract zip file, ' + str(e)}), 500

    # 删除上传的zip文件以释放空间
    os.remove(zip_path)

    # 读取元数据文件（如存在）
    metadata_path = os.path.join(workdir, 'metadata.json')
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r') as file:
            metadata = json.load(file)
        namespace = metadata['namespace']
        appname = metadata['name']
        images = metadata['images']
        print('Loaded metadata:', metadata, flush=True)
    else:
        metadata = {}

    new_workdir = os.path.join(SAVE_PATH, namespace, appname)
    if not os.path.exists(new_workdir):
        os.makedirs(new_workdir)

    # 移动 workdir 下的所有内容到 new_workdir
    for item in os.listdir(workdir):
        src_path = os.path.join(workdir, item)
        dest_path = os.path.join(new_workdir, item)
        shutil.move(src_path, dest_path)

    # 删除工作目录
    os.rmdir(workdir)

    deploy_response = upload_deploy_helper(new_workdir, namespace, appname, images)

    return deploy_response

# API端点：部署应用程序
@app.route('/api/deployAppWithImage', methods=['POST'])
def deploy_app_with_image():
    # 获取请求参数
    file_path = request.json.get('path')
    if not file_path:
        return jsonify({'error': 'Path is required'}), 400  
    ports = request.json.get('ports')
    if not ports:
        return jsonify({'error': 'Ports are required'}), 400
    namespace = request.args.get('namespace')
    appname = request.args.get('appname')  # 获取新的appname参数
    
    with open(os.path.join(file_path, 'metadata.json'), 'r') as file:
        metadata = json.load(file)
    old_appname = metadata['name']  # 保存原始appname用于替换
    if not namespace:
        namespace = metadata['namespace']
    if not appname:
        appname = old_appname  # 如果没有提供新的appname，使用原来的
        
    images = metadata['images']
    for image in images:
        image['path'] = os.path.join(file_path, image['path'].split('/')[-1])
    with open(os.path.join(file_path, 'app.yaml'), 'r') as file:
        yaml_content = file.read()

    new_yaml_contents = []
    for single_yaml in yaml.safe_load_all(yaml_content):
        # 替换metadata.name
        if single_yaml.get('metadata', {}).get('name') == old_appname:
            single_yaml['metadata']['name'] = appname
            
        # 替换labels中的app名称
        labels = single_yaml.get('metadata', {}).get('labels', {})
        if labels.get('cloud.sealos.io/app-deploy-manager') == old_appname:
            labels['cloud.sealos.io/app-deploy-manager'] = appname
        if labels.get('app') == old_appname:
            labels['app'] = appname
            
        # 替换selector中的app名称
        if single_yaml.get('kind') == 'Service':
            if 'selector' in single_yaml['spec']:
                if single_yaml['spec']['selector'].get('app') == old_appname:
                    single_yaml['spec']['selector']['app'] = appname
                    
        if single_yaml.get('kind') == 'Deployment':
            # 替换deployment selector中的matchLabels
            if 'selector' in single_yaml['spec']:
                if single_yaml['spec']['selector'].get('matchLabels', {}).get('app') == old_appname:
                    single_yaml['spec']['selector']['matchLabels']['app'] = appname
            
            # 替换template labels中的app名称
            if 'template' in single_yaml['spec']:
                template_labels = single_yaml['spec']['template'].get('metadata', {}).get('labels', {})
                if template_labels.get('app') == old_appname:
                    template_labels['app'] = appname
        
        # 处理NodePort和其他已有的逻辑
        if 'kind' in single_yaml and single_yaml['kind'] == 'Service':
            if 'spec' in single_yaml and 'type' in single_yaml['spec'] and single_yaml['spec']['type'] == 'NodePort':
                for port_index in range(len(single_yaml['spec']['ports'])):
                    internal_port = str(single_yaml['spec']['ports'][port_index]['port'])
                    if internal_port not in ports.keys():
                        return jsonify({'error': 'ExternalPort for InternalPort ' + internal_port + ' is required'}), 400
                    if not isinstance(ports[internal_port], int):
                        return jsonify({'error': 'ExternalPort for InternalPort ' + internal_port + ' should be int'}), 400
                    if ports[internal_port] < 30000 or ports[internal_port] > 32767:
                        return jsonify({'error': 'ExternalPort for InternalPort ' + internal_port + ' should be between 30000 and 32767'}), 400
                    single_yaml['spec']['ports'][port_index]['nodePort'] = ports[internal_port]
                    
        if 'kind' in single_yaml and single_yaml['kind'] == 'Deployment':
            if 'spec' in single_yaml and 'template' in single_yaml['spec'] and 'spec' in single_yaml['spec']['template']:
                if 'containers' in single_yaml['spec']['template']['spec']:
                    for container_index in range(len(single_yaml['spec']['template']['spec']['containers'])):
                        container = single_yaml['spec']['template']['spec']['containers'][container_index]
                        if 'image' in container:
                            if not '/' in container['image']:
                                container['image'] = 'library/' + container['image']
                            if not ':' in container['image']:
                                container['image'] = container['image'] + ':latest'
                                
        new_yaml_contents.append(single_yaml)
        
    new_yaml_content = yaml.dump_all(new_yaml_contents)

    print('deployAppWithImage, appname:', appname, 'namespace:', namespace, flush=True)

    # 加载和推送镜像
    for image in images:
        name = image['name'].strip()
        path = image['path']

        # 登录镜像仓库
        err = run_command('docker login -u admin -p passw0rd sealos.hub:5000')
        if err:
            return jsonify({'error': 'Failed to login, ' + err}), 500

        # 加载镜像
        err = run_command('docker load -i ' + path)
        if err:
            return jsonify({'error': 'Failed to load image, ' + err}), 500
        # 替换域名并推送镜像
        parts = name.split('/')
        if len(parts) == 3:
            new_name = 'sealos.hub:5000/' + '/'.join(parts[1:])
        elif len(parts) == 1:
            new_name = 'sealos.hub:5000/library/' + name
        elif len(parts) == 2:
            new_name = 'sealos.hub:5000/' + name
        else:
            return jsonify({'error': 'Invalid image name: ' + name}), 400
        err = run_command('docker tag ' + name + ' ' + new_name)
        if err:
            return jsonify({'error': 'Failed to tag image, ' + err}), 500
        err = run_command('docker push ' + new_name)
        if err:
            return jsonify({'error': 'Failed to push image, ' + err}), 500

    # 替换yaml中的CLUSTER_DOMAIN
    new_yaml_content = new_yaml_content.replace('CLUSTER_DOMAIN', CLUSTER_DOMAIN)
    with open('temp.yaml', 'w') as file:
        file.write(new_yaml_content)

    # 调用kubectl创建命名空间
    create_namespace_command = 'kubectl create namespace ' + namespace + ' --kubeconfig=/etc/kubernetes/admin.conf'
    err = run_command(create_namespace_command)

    if err:
        if 'already exists' not in err:
            return jsonify({'error': 'Failed to create namespace, ' + err}), 500

    # 调用kubectl部署应用
    apply_command = 'kubectl apply -n ' + namespace + ' --kubeconfig=/etc/kubernetes/admin.conf -f temp.yaml'
    err = run_command(apply_command)

    if err:
        return jsonify({'error': 'Failed to apply application, ' + err}), 500

    # 返回成功响应
    detail_url = 'http://' + CLUSTER_DOMAIN + ':32293/app/detail?namespace=' + namespace + '&&name=' + appname
    return jsonify({'message': 'Application deployed successfully', 'url': detail_url}), 200

def run_command_loadAndPushImage(command):
    """执行命令并返回结果"""
    try:
        res = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return res
    except subprocess.CalledProcessError as e:
        print("Error executing command: " + e.stderr.decode().strip())
        return e.stderr.decode().strip()

# API端点：加载、标记并推送镜像
@app.route('/api/loadAndPushImage', methods=['POST'])
def load_and_push_image():
    # 获取请求参数
    image_name = request.form.get('image_name')
    if not image_name:
        return jsonify({'error': 'image_name is required'}), 400

    tag = request.form.get('tag')
    if not tag:
        return jsonify({'error': 'tag is required'}), 400

    namespace = request.form.get('namespace')
    if not namespace:
        return jsonify({'error': 'namespace is required'}), 400

    image_file = request.files.get('image_file')
    if not image_file:
        return jsonify({'error': 'image_file is required'}), 400

    # 检查并创建保存路径
    workdir = os.path.join(SAVE_PATH, 'temp')
    os.makedirs(workdir, exist_ok=True)

    # 保存上传的镜像文件
    image_path = os.path.join(workdir, image_file.filename)
    image_file.save(image_path)
    print('Saved image file to: {}'.format(image_path), flush=True)

    try:
        # 加载镜像并获取镜像的名称
        load_output = run_command_loadAndPushImage('docker load -i {}'.format(image_path))
        print('load_output: {}'.format(load_output))
        
        if isinstance(load_output, subprocess.CompletedProcess):
            load_output_str = load_output.stdout.decode().strip()
        else:
            load_output_str = load_output

        # 确认加载输出中是否包含 'Loaded' 字样
        if 'Loaded' not in load_output_str:
            return jsonify({'error': 'Failed to load image: ' + load_output_str}), 500
        print("Loaded image from {}".format(image_path), flush=True)

        # 从docker load的输出中提取镜像名称
        # 例子输出: Loaded image: sealos.hub:5000/pause:3.6
        base_image_name = load_output_str.split('Loaded image: ')[-1]
        print("Base image name extracted: {}".format(base_image_name), flush=True)

        # 给镜像打标签，并加上命名空间
        full_image_name = 'sealos.hub:5000/{}/{}:{}'.format(namespace, image_name, tag)
        docker_tag_command = 'docker tag {} {}'.format(base_image_name, full_image_name)
        print("Running command: {}".format(docker_tag_command))  # 打印出完整命令
        err = run_command_loadAndPushImage(docker_tag_command)

        # 判断是否出错
        if isinstance(err, subprocess.CalledProcessError):
            error_message = err.stderr.decode().strip()  # 获取标准错误信息
            print("Error during docker tag: {}".format(error_message))
            return jsonify({'error': 'Failed to tag image: ' + error_message}), 500

        print("Tagged image as {}".format(full_image_name), flush=True)

        # 推送镜像到 sealos.hub
        docker_push_command = 'docker push {}'.format(full_image_name)
        print("Running push command: {}".format(docker_push_command))
        err = run_command_loadAndPushImage(docker_push_command)

        # 判断推送是否成功
        if isinstance(err, subprocess.CalledProcessError):
            error_message = err.stderr.decode().strip()  # 获取标准错误信息
            print("Error during docker push: {}".format(error_message))
            return jsonify({'error': 'Failed to push image: ' + error_message}), 500

        print("Pushed image to {}".format(full_image_name), flush=True)

    finally:
        # 确保删除临时镜像文件
        if os.path.exists(image_path):
            os.remove(image_path)

    # 返回成功响应
    return jsonify({'message': 'Image {} loaded, tagged, and pushed successfully'.format(full_image_name)}), 200

def get_cluster_resources():
    """获取集群资源使用情况（基于limits）"""
    try:
        # 获取节点总容量
        cmd = "kubectl get nodes --no-headers -o custom-columns=':status.capacity.cpu',':status.capacity.memory' --kubeconfig=/etc/kubernetes/admin.conf"
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        
        # 解析节点容量
        lines = result.stdout.strip().split('\n')
        total_cpu = 0
        total_memory = 0
        
        for line in lines:
            cap_cpu, cap_mem = line.split()
            total_cpu += float(re.sub(r'[^0-9.]', '', cap_cpu))
            
            # 转换内存值为Gi
            mem_gi = float(re.sub(r'[^0-9.]', '', cap_mem))
            if 'Ki' in cap_mem:
                mem_gi /= 1048576
            elif 'Mi' in cap_mem:
                mem_gi /= 1024
            total_memory += mem_gi

        # 获取所有Pod的资源限制
        cmd = "kubectl get pods --all-namespaces -o json --kubeconfig=/etc/kubernetes/admin.conf"
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        pods = json.loads(result.stdout)

        total_cpu_limits = 0
        total_memory_limits = 0

        for pod in pods['items']:
            if pod['status']['phase'] in ['Running', 'Pending']:
                containers = pod['spec'].get('containers', [])
                for container in containers:
                    limits = container.get('resources', {}).get('limits', {})
                    
                    # 计算CPU限制
                    if 'cpu' in limits:
                        cpu_limit = limits['cpu']
                        if cpu_limit.endswith('m'):
                            total_cpu_limits += float(cpu_limit[:-1]) / 1000
                        else:
                            total_cpu_limits += float(cpu_limit)

                    # 计算内存限制
                    if 'memory' in limits:
                        mem_limit = limits['memory']
                        mem_value = float(re.sub(r'[^0-9.]', '', mem_limit))
                        if 'Ki' in mem_limit:
                            total_memory_limits += mem_value / 1048576
                        elif 'Mi' in mem_limit:
                            total_memory_limits += mem_value / 1024
                        elif 'Gi' in mem_limit:
                            total_memory_limits += mem_value
                        elif 'Ti' in mem_limit:
                            total_memory_limits += mem_value * 1024

        # 计算资源使用百分比（基于limits）
        cpu_usage_percent = (total_cpu_limits / total_cpu) * 100
        memory_usage_percent = (total_memory_limits / total_memory) * 100
        
        print("Cluster resources - CPU: {}%, Memory: {}%".format(cpu_usage_percent, memory_usage_percent), flush=True)
        
        return cpu_usage_percent, memory_usage_percent
    except Exception as e:
        print("Error getting cluster resources: {}".format(str(e)))
        return None, None

def scale_high_priority_workloads():
    """检查资源使用情况并根据需要缩放工作负载"""
    try:
        cpu_usage, memory_usage = get_cluster_resources()
        if cpu_usage is None or memory_usage is None:
            return
        
        # 如果CPU或内存使用率超过RESOURCE_THRESHOLD
        if cpu_usage > float(RESOURCE_THRESHOLD) or memory_usage > float(RESOURCE_THRESHOLD):
            print("Resource usage is high - CPU: {}%, Memory: {}%".format(cpu_usage, memory_usage))
            
            # 获取所有deployment和statefulset
            workload_types = ['deployment', 'statefulset']
            for workload_type in workload_types:
                cmd = "kubectl get {} --all-namespaces -o json --kubeconfig=/etc/kubernetes/admin.conf".format(workload_type)
                result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
                workloads = json.loads(result.stdout)
                
                for workload in workloads['items']:
                    labels = workload['metadata'].get('labels', {})
                    priority = labels.get('deploy.cloud.sealos.io/priority', '')
                    
                    try:
                        priority_value = int(priority)
                        if priority_value > 1:
                            namespace = workload['metadata']['namespace']
                            name = workload['metadata']['name']
                            
                            # 调用暂停应用的接口
                            pause_url = "http://{}:32293/api/pauseApp?namespace={}&&appName={}&&isStop=none".format(CLUSTER_DOMAIN, namespace, name)
                            response = requests.get(pause_url)
                            
                            if response.status_code == 200:
                                print("Paused {} {}/{} successfully".format(workload_type, namespace, name))
                            else:
                                print("Failed to pause {} {}/{}: {}".format(workload_type, namespace, name, response.text))
                    except ValueError:
                        continue
    except Exception as e:
        print("Error in scale_high_priority_workloads: {}".format(str(e)))

# 创建定时任务调度器
scheduler = BackgroundScheduler()
scheduler.add_job(scale_high_priority_workloads, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5002)
    finally:
        scheduler.shutdown()

