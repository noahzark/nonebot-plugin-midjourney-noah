from .base import AIDRAW_BASE
from ..config import config
import time
from ..utils.load_balance import sd_LoadBalance, get_vram
from ..utils import get_generate_info
from nonebot import logger
from copy import deepcopy
import json, aiofiles
import asyncio
import traceback
import random
import ast


header = {
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/106.0.0.0 Safari/537.36",
}


class AIDRAW(AIDRAW_BASE):
    """队列中的单个请求"""
    max_resolution: int = 32
    
    async def get_model_index(self, model_name, models_dict):
        reverse_dict = {value: key for key, value in models_dict.items()}
        for model in list(models_dict.values()):
            if model_name in model:
                model_index = reverse_dict[model]
                return model_index
    
    async def fromresp(self, resp):
        img: dict = await resp.json()
        return img["images"][0]
    
    async def load_balance_init(self):
        '''
        负载均衡初始化
        '''
        if self.control_net["control_net"]:
            self.task_type = "controlnet"
        elif self.img2img:
            self.task_type = "img2img"
        else:
            self.task_type = "txt2img"
        logger.info(f"任务类型:{self.task_type}")
        resp_tuple = await sd_LoadBalance()
        self.backend_name = resp_tuple[1][1]
        self.backend_site = resp_tuple[1][0]
        return resp_tuple

    async def post_parameters(self):
        '''
        获取post参数
        '''
        global site
        if self.backend_index is not None and isinstance(self.backend_index, int):
            self.backend_site = list(config.novelai_backend_url_dict.values())[self.backend_index]
            self.backend_name = config.backend_name_list[self.backend_index]
        if self.backend_site:
            site = self.backend_site
        else:
            if config.novelai_load_balance:
                await self.load_balance_init()
                site = self.backend_site or defult_site 
            else:
                site = defult_site or await config.get_value(self.group_id, "site") or config.novelai_site or "127.0.0.1:7860"

        post_api = f"http://{site}/sdapi/v1/img2img" if self.img2img else f"http://{site}/sdapi/v1/txt2img"
        
        parameters = {
            "prompt": self.tags,
            "seed": self.seed,
            "steps": self.steps,
            "cfg_scale": self.scale,
            "width": self.width,
            "height": self.height,
            "negative_prompt": self.ntags,
            "sampler_name": self.sampler,
            "denoising_strength": self.strength,
            "save_images": config.save_img,
            "alwayson_scripts": {}
        }
        
        if self.model_index:
            from ..extension.sd_extra_api_func import sd
            model_dict = await sd(self.backend_index or config.backend_site_list.index(self.backend_site), True)
            self.model_index = self.model_index if self.model_index.isdigit() else await self.get_model_index(self.model_index, model_dict)
            if self.is_random_model:
                from ..extension.sd_extra_api_func import sd
                self.model_index = random.randint(1, len(list(model_dict.keys())))
            self.model = model_dict[int(self.model_index)]
            parameters.update(
                {"override_settings": {"sd_model_checkpoint": self.model}, 
                "override_settings_restore_afterwards": "true"}
            )
        if self.img2img:
            if self.control_net["control_net"] and config.novelai_hr:
                parameters.update(self.novelai_hr_payload)
            parameters.update({
                "init_images": ["data:image/jpeg;base64,"+self.image],
                "denoising_strength": self.strength,
            }
            ) 
        else:
            if config.novelai_hr and self.disable_hr is False:
                parameters.update(self.novelai_hr_payload)
            else:
                self.hiresfix = False
        if self.xyz_plot:
            input_str_replaced = self.xyz_plot.replace('""', 'None')
            try:
                xyz_list = ast.literal_eval('[' + input_str_replaced + ']')
            except (SyntaxError, ValueError):
                xyz_list = []
            xyz_list = ["" if item is None else item for item in xyz_list]
            parameters.update({"script_name": "x/y/z plot", "script_args": xyz_list})
            # if "_" and "," in self.xyz_plot:
            #     result = [config.scripts[0]["args"][i:i+3] for i in range(0, len(config.scripts[0]["args"]), 3)]
            #     axes = self.xyz_plot.split(",")
            #     args = axes.split("_")
            #     result[0][0] = args[0]
            #     args[0] = 
        if self.open_pose:
            parameters.update({"enable_hr": "false"})
            parameters["steps"] = 12
        if self.td or config.tiled_diffusion:
            parameters["alwayson_scripts"].update(config.custom_scripts[0])
        if self.sag:
            parameters["alwayson_scripts"].update(config.custom_scripts[2]) 
        if self.custom_scripts is not None:
            parameters["alwayson_scripts"].update(config.custom_scripts[self.custom_scripts])
        if self.scripts is not None:
            parameters.update({"script_name": config.scripts[self.scripts]["name"], "script_args": config.scripts[self.scripts]["args"]})
        if self.control_net["control_net"] == True and config.novelai_hr:
            if config.hr_off_when_cn:
                parameters.update({"enable_hr": "false"})
            else:
                org_scale = parameters["hr_scale"]
                parameters.update({"hr_scale": org_scale * 0.75}) # control较吃显存, 高清修复倍率恢复为1.5
            del parameters["init_images"]
            if config.novelai_ControlNet_post_method == 0:
                post_api = f"http://{site}/sdapi/v1/txt2img"
                parameters.update(config.novelai_ControlNet_payload[0])
                parameters["alwayson_scripts"]["controlnet"]["args"][0]["input_image"] = self.image
            else:
                post_api = f"http://{site}/controlnet/txt2img"
                parameters.update(config.novelai_ControlNet_payload[1])
                parameters["controlnet_units"][0]["input_image"] = self.image
        logger.debug(str(parameters))        
        self.post_parms = parameters
        return header, post_api, parameters

    async def post(self):
        global defult_site
        defult_site = None # 所有后端失效后, 尝试使用默认后端
        # 失效自动重试 
        for retry_times in range(config.novelai_retry):
            try:
                self.start_time = time.time()
                parameters_tuple = await self.post_parameters()
                await self.post_(*parameters_tuple)
            except Exception:
                self.start_time: float = time.time()
                logger.info(f"第{retry_times + 1}次尝试")
                logger.error(traceback.print_exc())
                await asyncio.sleep(2)
                if retry_times >= 1: # 如果指定了后端, 重试两次仍然失败的话, 使用负载均衡重新获取可用后端
                    defult_site = config.novelai_site
                    self.backend_index = None
                    self.backend_site = None
                    await asyncio.sleep(30) 
                if retry_times > config.novelai_retry:
                    raise RuntimeError(f"重试{config.novelai_retry}次后仍然发生错误, 请检查服务器")
            else:
                if config.novelai_load_balance is False:
                    try:
                        self.backend_name = (
                            list(config.novelai_backend_url_dict.keys())[self.backend_index] 
                            if self.backend_index 
                            else self.backend_name
                        )
                    except Exception:
                        self.backend_name = ""
                resp_list = await asyncio.gather(
                    *[self.get_webui_config(self.backend_site), 
                    get_vram(self.backend_site)], 
                    return_exceptions=False
                )
                resp_json = resp_list[0]
                try:
                    if self.model is None:
                        self.model = resp_json["sd_model_checkpoint"]
                except Exception:
                    self.model = ""
                self.vram = resp_list[1]
                break
        generate_info = get_generate_info(self, "生成完毕")
        logger.info(
            f"{generate_info}")
        return self.result