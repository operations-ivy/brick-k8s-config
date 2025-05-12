# MAIN NODE

- k3s-uninstall.sh

- ifconfig get private non routable IP (INTERNAL_IP)

- export INTERNAL_IP=ip address

- curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --disable=traefik --flannel-backend=host-gw --tls-san=$INTERNAL_IP --bind-address=$INTERNAL_IP --advertise-address=$INTERNAL_IP --node-ip=$INTERNAL_IP --cluster-init" sh -s -

- get token from main node

- sudo cat /var/lib/rancher/k3s/server/node-token

- sudo cp /etc/rancher/k3s/k3s.yaml $HOME/config

- sudo chown zaphod:zaphod config



# AGENT NODE

- k3s-agent-uninstall.sh

- export NODE_TOKEN=token from main noded

- curl -sfL https://get.k3s.io | K3S_URL=https://main_node_internal_ip:6443 K3S_TOKEN=$NODE_TOKEN sh -s



# LOCALHOST

- scp zaphod@wintermute.local://home/zaphod/config $HOME/.kube/config

- kubectl apply -f https://downloads.portainer.io/ce2-27/portainer-agent-k8s-lb.yaml

- kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/cloud/deploy.yaml
