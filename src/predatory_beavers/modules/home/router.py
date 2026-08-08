from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from predatory_beavers.api.responses import ApiResponse
from predatory_beavers.modules.home.schemas import HomeData
from predatory_beavers.modules.home.service import HomeService

router = APIRouter(route_class=DishkaRoute, tags=["home"])


@router.get("/home", response_model=ApiResponse[HomeData])
async def get_home(service: FromDishka[HomeService]) -> ApiResponse[HomeData]:
    return ApiResponse(message="Home data retrieved", data=await service.get())
